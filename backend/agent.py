"""OpenAI-powered reply agent for incoming iMessages."""

from __future__ import annotations

import os
from collections.abc import Iterable

from openai import AsyncOpenAI

SYSTEM_PROMPT = (
    "You are a friendly, concise assistant replying to text messages on behalf "
    "of the user. Keep replies under two short sentences, casual, and direct. "
    "Never claim to be human; if asked, say you're an AI assistant relaying "
    "messages. Match the tone of the incoming message."
)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI()
    return _client


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


async def generate_reply(history: Iterable[tuple[str, str]]) -> str:
    """`history` is a sequence of (role, text) where role is "inbound" or "outbound".

    Returns the agent's reply text. Raises on API errors so the caller can log.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for role, text in history:
        if not text:
            continue
        mapped = "user" if role == "inbound" else "assistant"
        messages.append({"role": mapped, "content": text})

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    response = await _get_client().chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=120,
        temperature=0.7,
    )
    return (response.choices[0].message.content or "").strip()
