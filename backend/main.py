import asyncio
import json
from collections import deque
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

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
