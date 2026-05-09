"""OpenAI-powered reply agent for incoming iMessages.

Uses chat completions with tool-calling so Rachel can autonomously call
`track_company`, `search_reddit`, `search_x`, `search_linkedin` when she's
collected enough info from the user.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from openai import AsyncOpenAI

import tools

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI()
    return _client


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


# Up to this many tool round-trips before we force a final reply.
MAX_TOOL_ROUNDS = 4


async def generate_reply(
    history: Iterable[tuple[str, str]], *, participant: str
) -> str:
    """Run a tool-calling chat completion. Returns the final assistant text.

    `history` is a sequence of (role, text) where role is "inbound" (user) or
    "outbound" (assistant). `participant` is threaded into tool calls so each
    tool knows which conversation/run it belongs to.
    """
    messages: list[dict] = [{"role": "system", "content": _load_prompt()}]
    for role, text in history:
        if not text:
            continue
        mapped = "user" if role == "inbound" else "assistant"
        messages.append({"role": mapped, "content": text})

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = _get_client()

    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools.TOOL_DEFS,
            tool_choice="auto",
            temperature=0.7,
            max_tokens=300,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            return (msg.content or "").strip()

        # Append assistant's tool-call message, then run each tool and append results.
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )
        for tc in msg.tool_calls:
            result = await tools.dispatch(
                tc.function.name, tc.function.arguments, participant=participant
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )

    # Bail-out: ask the model for a plain reply with tools disabled.
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.7,
        max_tokens=200,
    )
    return (response.choices[0].message.content or "").strip()
