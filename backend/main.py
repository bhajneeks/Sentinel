import asyncio
import json
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import agent
import imessage

load_dotenv(".env.local")
load_dotenv()  # fallback to .env

logger = logging.getLogger("uvicorn.error")

MAX_BUFFERED = 500
HISTORY_FOR_REPLY = 12

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
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
        reply_text = await agent.generate_reply(history)
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


@app.post("/api/messages/ingest", status_code=204)
async def ingest_message(message: Message):
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
