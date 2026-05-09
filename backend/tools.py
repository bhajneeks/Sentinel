"""OpenAI tool layer.

Rachel (the chat agent) gets four tools:

- track_company(company, link) — opens a fresh tracking run for the active conversation
- search_reddit(query)        — Reddit JSON-API scraper, fire-and-forget
- search_x(query)             — browser-use scraper for X (counts toward 25-browser cap)
- search_linkedin(query)      — browser-use scraper for LinkedIn (counts toward cap)

The tools return short status strings so the LLM can narrate what's happening.
The actual work happens in background tasks that stream events + mentions to
Convex. The frontend subscribes to those tables and injects findings into the
chat as Rachel's own messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import convex_client as cx
import scraper_stream

logger = logging.getLogger("uvicorn.error")

# participant -> active agentRuns _id (Convex doc id)
_active_run_by_participant: dict[str, str] = {}


def get_active_run(participant: str) -> str | None:
    return _active_run_by_participant.get(participant)


# ── tool schemas ──────────────────────────────────────────────────────────────


TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "track_company",
            "description": (
                "Open a tracking run for the current conversation. Call this "
                "as soon as you have BOTH a company name AND a link from the "
                "user. After this returns, you can call search_reddit / "
                "search_x / search_linkedin to actually pull mentions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "The company name to track.",
                    },
                    "link": {
                        "type": "string",
                        "description": "Any URL the user gave (homepage, social, news, etc.).",
                    },
                },
                "required": ["company", "link"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_reddit",
            "description": (
                "Search Reddit for posts and comments mentioning a query. "
                "Returns immediately; mentions stream into the UI as they're "
                "found. Requires a prior track_company call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search term (e.g. the company name or a related phrase).",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_x",
            "description": (
                "Open a real browser to X (Twitter) and pull live posts "
                "matching a query. Counts toward the 25-concurrent-browser "
                "cap. Returns immediately; mentions stream as they're found."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_linkedin",
            "description": (
                "Open a real browser to LinkedIn and pull recent posts "
                "matching a query. Counts toward the 25-concurrent-browser "
                "cap. Returns immediately; mentions stream as they're found."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
]


# ── dispatch ──────────────────────────────────────────────────────────────────


async def dispatch(name: str, raw_args: str, *, participant: str) -> str:
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as exc:
        return f"error: bad JSON args ({exc})"

    handler = _HANDLERS.get(name)
    if handler is None:
        return f"error: unknown tool {name!r}"

    try:
        return await handler(args, participant=participant)
    except Exception as exc:  # surfaces back to the LLM
        logger.exception("tool %s crashed", name)
        return f"error: {exc}"


# ── handlers ──────────────────────────────────────────────────────────────────


async def _track_company(args: dict[str, Any], *, participant: str) -> str:
    company = (args.get("company") or "").strip()
    link = (args.get("link") or "").strip()
    if not company or not link:
        return "error: need both company and link"

    run_id = await cx.create_run(participant, company, link)
    _active_run_by_participant[participant] = run_id
    return (
        f"started run {run_id} for {company} ({link}). "
        "you can now call search_reddit / search_x / search_linkedin."
    )


async def _search_platform(
    args: dict[str, Any],
    *,
    participant: str,
    platform: str,
    browser_backed: bool,
) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "error: query is required"

    run_id = _active_run_by_participant.get(participant)
    if not run_id:
        return "error: no active tracking run. call track_company first."

    if browser_backed:
        active = await cx.active_browser_count()
        if active >= cx.BROWSER_CONCURRENCY_CAP:
            return (
                f"error: 25-browser cap reached ({active} active). "
                "wait for some to finish, then retry."
            )

    session_id = await cx.start_session(
        run_id, platform, query, browser_backed=browser_backed
    )

    asyncio.create_task(
        scraper_stream.run_scraper(
            run_id=run_id,
            session_id=session_id,
            platform=platform,
            query=query,
        )
    )
    return f"started {platform} scrape for '{query}' (session {session_id})"


async def _search_reddit(args: dict[str, Any], *, participant: str) -> str:
    return await _search_platform(
        args, participant=participant, platform="reddit", browser_backed=False
    )


async def _search_x(args: dict[str, Any], *, participant: str) -> str:
    return await _search_platform(
        args, participant=participant, platform="x", browser_backed=True
    )


async def _search_linkedin(args: dict[str, Any], *, participant: str) -> str:
    return await _search_platform(
        args, participant=participant, platform="linkedin", browser_backed=True
    )


_HANDLERS = {
    "track_company": _track_company,
    "search_reddit": _search_reddit,
    "search_x": _search_x,
    "search_linkedin": _search_linkedin,
}
