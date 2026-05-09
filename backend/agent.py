"""OpenAI-powered reply agent for incoming iMessages.

Also exposes a generic `chat_completion()` so other backend modules
(e.g. `campaign.py`) can reuse the configured client without rebuilding
their own OpenAI plumbing.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

_PROMPT_PATH = Path(__file__).parent / "prompt.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

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


async def chat_completion(
    *,
    messages: list[dict[str, Any]],
    max_tokens: int = 800,
    temperature: float = 0.7,
    model: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> str:
    """Generic chat completion for non-iMessage callers (e.g. campaign orchestrator)."""
    chosen = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    kwargs: dict[str, Any] = {
        "model": chosen,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    response = await _get_client().chat.completions.create(**kwargs)
    return (response.choices[0].message.content or "").strip()
