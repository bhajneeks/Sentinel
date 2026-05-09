import asyncio
import json
import logging
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

load_dotenv(".env.local")
load_dotenv()  # fallback to .env

import agent  # noqa: E402  -- must come after load_dotenv()
import campaign  # noqa: E402  -- must come after load_dotenv()
import imessage  # noqa: E402  -- must come after load_dotenv()
from overshoot import (  # noqa: E402  -- must come after load_dotenv()
    DEFAULT_MODEL as OVERSHOOT_DEFAULT_MODEL,
    OvershootAPIError,
    OvershootConfigError,
    analyze_promo_video,
)
from reacher import (  # noqa: E402  -- must come after load_dotenv()
    ALLOWED_SORT_BY,
    ALLOWED_TIME_RANGES,
    ReacherAPIError,
    ReacherConfigError,
    get_competitor_landscape,
    get_trending_videos,
)
from scraper import scrape_x, scrape_linkedin, scrape_reddit, Mention  # noqa: E402
import monitor_store  # noqa: E402
from monitor_agent import _run as _monitor_run  # noqa: E402

logger = logging.getLogger("uvicorn.error")

MAX_BUFFERED = 500
HISTORY_FOR_REPLY = 12

# ── Monitor scheduler ─────────────────────────────────────────────────────────

_scheduler = AsyncIOScheduler()
_active_cfg: "MonitorConfig | None" = None


async def _scheduled_run() -> None:
    if _active_cfg is None:
        return
    try:
        await _monitor_run(
            _active_cfg.brand_terms,
            _active_cfg.lookback_minutes,
            _active_cfg.alert_to,
            expand_if_quiet=True,
        )
    except Exception as exc:
        logger.error("monitor scheduled run failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import monitor_agent
    _scheduler.start()
    yield
    _scheduler.shutdown(wait=False)
    monitor_agent._executor.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_methods=["*"],
    allow_headers=["*"],
)


Direction = Literal["inbound", "outbound"]
ChatKind = Literal["dm", "group", "unknown"]


class Message(BaseModel):
    id: str
    text: str | None = None
    participant: str | None = None
    chatId: str | None = None
    chatKind: ChatKind = "unknown"
    service: str | None = None
    createdAt: datetime
    direction: Direction = "inbound"


class ConversationSummary(BaseModel):
    participant: str
    chatKind: ChatKind
    lastMessageAt: datetime
    lastMessageText: str | None
    lastDirection: Direction
    messageCount: int


messages: deque[Message] = deque(maxlen=MAX_BUFFERED)
subscribers: set[asyncio.Queue[Message]] = set()
closed_participants: set[str] = set()


def broadcast(message: Message) -> None:
    for queue in list(subscribers):
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass


def conversation_history(participant: str, limit: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in messages:
        if m.participant != participant or not m.text:
            continue
        out.append((m.direction, m.text))
    return out[-limit:]


async def _generate_and_broadcast(source: Message) -> None:
    participant = source.participant
    if not participant:
        return
    history = conversation_history(participant, HISTORY_FOR_REPLY)
    if not history:
        return
    try:
        reply_text = await agent.generate_reply(history, participant=participant)
    except Exception as exc:  # network / auth / rate limit
        logger.warning("agent reply failed: %s", exc)
        return
    if not reply_text:
        return

    fragments = [p.strip() for p in reply_text.split("||") if p.strip()]
    for fragment in fragments:
        await imessage.send(participant, fragment, source.service)
        reply = Message(
            id=f"agent-{uuid.uuid4().hex[:12]}",
            text=fragment,
            participant=participant,
            chatId=source.chatId,
            chatKind="dm",
            service=source.service or "iMessage",
            createdAt=datetime.now(timezone.utc),
            direction="outbound",
        )
        messages.append(reply)
        broadcast(reply)


@app.get("/api/hello")
def hello():
    return {"message": "f from FastAPI"}


@app.get("/api/trending-videos")
async def trending_videos(
    interest: str = Query(..., min_length=1, max_length=200,
                          description="What the user is interested in (e.g. 'skincare')."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: str | None = Query(None, max_length=100),
    sort_by: str = Query("views", pattern=f"^({'|'.join(ALLOWED_SORT_BY)})$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    time_range: str = Query(
        "7 days",
        pattern=f"^({'|'.join(ALLOWED_TIME_RANGES)})$",
    ),
    shop_id: str | None = Query(
        None, max_length=64,
        description="Override REACHER_SHOP_ID for this request. Pass 'all' to aggregate.",
    ),
):
    try:
        return await get_trending_videos(
            interest,
            page=page,
            page_size=page_size,
            category=category,
            sort_by=sort_by,
            sort_order=sort_order,
            time_range=time_range,
            shop_id=shop_id,
        )
    except ReacherConfigError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except ReacherAPIError as e:
        raise HTTPException(status_code=e.status, detail=e.body)


class AnalyzeVideoRequest(BaseModel):
    source: str = Field(
        ...,
        min_length=1,
        max_length=2048,
        description="Local file path or URL ffmpeg can open (mp4/HLS/etc).",
    )
    model: str = Field(
        OVERSHOOT_DEFAULT_MODEL,
        max_length=200,
        description="Overshoot model id, e.g. 'google/gemma-4-31B-it'.",
    )
    hook_window_ms: int = Field(
        3000, ge=500, le=15000,
        description="How much of the opening to count as the hook.",
    )
    publish_fps: int = Field(
        5, ge=1, le=15,
        description="Frames per second forwarded into the LiveKit room.",
    )
    max_height: int = Field(
        480, ge=144, le=1080,
        description="Downscale frames to this height before publishing.",
    )
    playback_speed: float = Field(
        4.0, ge=1.0, le=20.0,
        description="1.0 = realtime publish; higher = faster but compressed stream-clock.",
    )


@app.post("/api/analyze-video")
async def analyze_video(req: AnalyzeVideoRequest):
    """Watch a promo video through Overshoot and return hook + layout analysis."""
    try:
        return await analyze_promo_video(
            req.source,
            model=req.model,
            hook_window_ms=req.hook_window_ms,
            publish_fps=req.publish_fps,
            max_height=req.max_height,
            playback_speed=req.playback_speed,
        )
    except OvershootConfigError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except OvershootAPIError as e:
        raise HTTPException(status_code=e.status, detail=e.body)


@app.get("/api/competitors")
async def competitors(
    query: str = Query(..., min_length=1, max_length=200,
                       description="Free-text product description, e.g. 'glossy pink lip oil'."),
    top_products: int = Query(5, ge=1, le=20,
                              description="How many competitor products to return."),
    creators_per_product: int = Query(10, ge=1, le=50),
    videos_per_product: int = Query(10, ge=1, le=50),
    time_range: str = Query(
        "30 days",
        pattern=f"^({'|'.join(ALLOWED_TIME_RANGES)})$",
        description="Window for product videos.",
    ),
    shop_id: str | None = Query(
        None, max_length=64,
        description="Override REACHER_SHOP_ID for this request. Single id only — 'all' is rejected by Reacher on these endpoints.",
    ),
):
    """For a free-text product input, return competitor products plus the
    creators and videos driving each one."""
    try:
        return await get_competitor_landscape(
            query,
            top_products=top_products,
            creators_per_product=creators_per_product,
            videos_per_product=videos_per_product,
            time_range=time_range,
            shop_id=shop_id,
        )
    except ReacherConfigError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except ReacherAPIError as e:
        raise HTTPException(status_code=e.status, detail=e.body)


class CampaignRequest(BaseModel):
    brief: str = Field(
        ...,
        min_length=4,
        max_length=2000,
        description=(
            "Free-text marketing brief, e.g. "
            "'Make a marketing campaign for a lip gloss product'."
        ),
    )
    product_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Optional Reacher product_id to benchmark against directly. "
            "When set, subagent 1 skips the catalog search and pulls creators + "
            "videos for this exact product. Useful when the LLM-extracted query "
            "is too niche to match the TikTok Shop catalog (e.g. specialty coffee)."
        ),
    )
    shop_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Override REACHER_SHOP_ID for this run. Pass 'all' only if not "
            "using product_id (per-product endpoints reject 'all')."
        ),
    )


@app.post("/api/marketing-campaign")
async def marketing_campaign(req: CampaignRequest):
    """Run the multi-subagent marketing-campaign orchestrator.

    Subagents (run in parallel after product extraction):
      1. Reacher products + creators
      2. Reacher trending video hooks
      3. Nozomio company context (brand overview + compressed past-campaign memory)

    The synthesized campaign + a compressed memory note are saved to
    `data/campaigns/` and `nia local sync` is fired in the background so the
    campaign becomes part of future runs' context.
    """
    if not agent.is_configured():
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY is not set — campaign agent cannot run.",
        )
    try:
        return await campaign.run_campaign_pipeline(
            req.brief, product_id=req.product_id, shop_id=req.shop_id,
        )
    except ReacherConfigError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except ReacherAPIError as e:
        raise HTTPException(status_code=e.status, detail=e.body)


@app.get("/api/messages")
def list_messages() -> list[Message]:
    return list(messages)


@app.get("/api/conversations")
def list_conversations() -> list[ConversationSummary]:
    by_participant: dict[str, ConversationSummary] = {}
    counts: dict[str, int] = {}
    for m in messages:
        if not m.participant:
            continue
        if m.participant in closed_participants:
            continue
        counts[m.participant] = counts.get(m.participant, 0) + 1
        prev = by_participant.get(m.participant)
        if prev is None or m.createdAt >= prev.lastMessageAt:
            by_participant[m.participant] = ConversationSummary(
                participant=m.participant,
                chatKind=m.chatKind,
                lastMessageAt=m.createdAt,
                lastMessageText=m.text,
                lastDirection=m.direction,
                messageCount=0,
            )
    for participant, summary in by_participant.items():
        summary.messageCount = counts[participant]
    return sorted(
        by_participant.values(), key=lambda c: c.lastMessageAt, reverse=True
    )


@app.post("/api/conversations/{participant}/close", status_code=204)
def close_conversation(participant: str):
    closed_participants.add(participant)


@app.post("/api/conversations/{participant}/reopen", status_code=204)
def reopen_conversation(participant: str):
    closed_participants.discard(participant)


def _derive_participant(message: Message) -> str | None:
    """The Photon SDK leaves `participant` null on some inbound messages even
    though `chatId` contains the handle (Apple format: 'service;-;handle').
    Recover it for DMs so Rachel can group the conversation correctly."""
    if message.participant:
        return message.participant
    if message.chatKind == "group":
        return None
    if not message.chatId:
        return None
    parts = message.chatId.split(";-;")
    if len(parts) == 2 and parts[1]:
        return parts[1]
    return None


@app.post("/api/messages/ingest", status_code=204)
async def ingest_message(message: Message):
    derived = _derive_participant(message)
    if derived and derived != message.participant:
        message = message.model_copy(update={"participant": derived})

    if message.participant in closed_participants:
        closed_participants.discard(message.participant)
    messages.append(message)
    broadcast(message)
    if (
        message.direction == "inbound"
        and message.participant
        and message.chatKind != "group"
        and agent.is_configured()
    ):
        asyncio.create_task(_generate_and_broadcast(message))


@app.get("/api/messages/stream")
async def stream_messages(request: Request):
    queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=MAX_BUFFERED)
    subscribers.add(queue)

    async def event_source():
        try:
            yield {
                "event": "snapshot",
                "data": json.dumps([m.model_dump(mode="json") for m in messages]),
            }
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
                    continue
                yield {
                    "event": "message",
                    "data": json.dumps(message.model_dump(mode="json")),
                }
        finally:
            subscribers.discard(queue)

    return EventSourceResponse(event_source())


@app.get("/api/agent/status")
def agent_status():
    return {"configured": agent.is_configured()}


class ScrapeRequest(BaseModel):
    brand_terms: list[str]
    lookback_minutes: int = 60
    seen_ids: list[str] = []


class ScrapeResponse(BaseModel):
    mentions: list[Mention]


@app.post("/api/scrape")
async def scrape(req: ScrapeRequest) -> ScrapeResponse:
    seen = set(req.seen_ids)
    results = await asyncio.gather(
        scrape_x(req.brand_terms, req.lookback_minutes, seen),
        scrape_linkedin(req.brand_terms, req.lookback_minutes, seen),
        scrape_reddit(req.brand_terms, req.lookback_minutes, seen),
    )
    mentions = [m for batch in results for m in batch]
    return ScrapeResponse(mentions=mentions)


# ── Monitor endpoints ─────────────────────────────────────────────────────────


class MonitorConfig(BaseModel):
    brand_terms: list[str] = Field(..., min_length=1)
    lookback_minutes: int = Field(60, ge=5, le=1440)
    alert_to: str | None = None
    interval_minutes: int = Field(15, ge=1, le=1440)


@app.post("/api/monitor/config")
def set_monitor_config(cfg: MonitorConfig):
    global _active_cfg
    _active_cfg = cfg
    _scheduler.remove_all_jobs()
    _scheduler.add_job(
        _scheduled_run,
        IntervalTrigger(minutes=cfg.interval_minutes),
        id="brand_monitor",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    return {"ok": True, "config": cfg.model_dump()}


@app.get("/api/monitor/status")
def monitor_status():
    state = monitor_store.load()
    job = _scheduler.get_job("brand_monitor")
    runs = state.get("runs", [])
    return {
        "enabled": _active_cfg is not None,
        "config": _active_cfg.model_dump() if _active_cfg else None,
        "last_run": runs[-1] if runs else None,
        "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "total_seen": len(state.get("seen_ids", [])),
        "signal_threshold": state.get("signal_threshold", 5),
        "quiet_runs": state.get("quiet_runs", 0),
        "last_alert_at": state.get("last_alert_at"),
        "tensorlake_active": True,
    }


@app.get("/api/monitor/history")
def monitor_history(limit: int = Query(50, ge=1, le=500)):
    state = monitor_store.load()
    return {
        "mentions": state.get("history", [])[-limit:][::-1],
        "runs": state.get("runs", [])[-20:][::-1],
        "baselines": {
            p: monitor_store.platform_baseline(state, p)
            for p in state.get("baselines", {})
        },
    }


@app.post("/api/monitor/trigger")
async def trigger_monitor():
    if _active_cfg is None:
        raise HTTPException(status_code=400, detail="Configure brand terms first via /api/monitor/config")
    return await _monitor_run(
        _active_cfg.brand_terms,
        _active_cfg.lookback_minutes,
        _active_cfg.alert_to,
        expand_if_quiet=True,
    )
