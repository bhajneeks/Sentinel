import asyncio
import json
from collections import deque
from datetime import datetime
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

load_dotenv()

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

MAX_BUFFERED = 200

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Message(BaseModel):
    id: str
    text: str | None = None
    participant: str | None = None
    chatId: str | None = None
    chatKind: Literal["dm", "group", "unknown"] = "unknown"
    service: str | None = None
    createdAt: datetime


messages: deque[Message] = deque(maxlen=MAX_BUFFERED)
subscribers: set[asyncio.Queue[Message]] = set()


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
        description="Overshoot model id, e.g. 'google/gemma-4-E4B-it'.",
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
        )
    except ReacherConfigError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except ReacherAPIError as e:
        raise HTTPException(status_code=e.status, detail=e.body)


@app.get("/api/messages")
def list_messages() -> list[Message]:
    return list(messages)


@app.post("/api/messages/ingest", status_code=204)
async def ingest_message(message: Message):
    messages.append(message)
    for queue in list(subscribers):
        # Drop on full so a slow client never blocks the watcher.
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass


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
