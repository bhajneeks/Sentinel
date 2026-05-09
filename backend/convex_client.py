"""Thin wrapper around the Convex Python client for the scraping pipeline.

All write paths in the backend (run/session/step/mention) go through here so
the rest of the code can stay unaware of the exact function names.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx
from convex import ConvexClient

logger = logging.getLogger("uvicorn.error")

# Concurrency cap on real-browser sessions across the whole backend.
BROWSER_CONCURRENCY_CAP = 25


_client: ConvexClient | None = None


def get_client() -> ConvexClient:
    global _client
    if _client is None:
        url = os.environ.get("CONVEX_URL")
        if not url:
            raise RuntimeError("CONVEX_URL is not set; cannot reach Convex")
        _client = ConvexClient(url)
    return _client


async def _run(fn, *args, **kwargs) -> Any:
    """ConvexClient is sync; run in a thread so we don't block the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ── runs ──────────────────────────────────────────────────────────────────────


async def create_run(participant: str, company: str, link: str) -> str:
    client = get_client()
    return await _run(client.mutation, "runs:create", {
        "participant": participant,
        "company": company,
        "link": link,
    })


async def finish_run(run_id: str, status: str, error: str | None = None) -> None:
    client = get_client()
    args: dict[str, Any] = {"runId": run_id, "status": status}
    if error:
        args["error"] = error
    await _run(client.mutation, "runs:finish", args)


# ── sessions ──────────────────────────────────────────────────────────────────


async def start_session(
    run_id: str, platform: str, query: str, browser_backed: bool
) -> str:
    client = get_client()
    return await _run(client.mutation, "sessions:start", {
        "runId": run_id,
        "platform": platform,
        "query": query,
        "browserBacked": browser_backed,
    })


async def start_cloud_session(
    *,
    platform: str,
    query: str,
    live_url: str,
    cloud_session_id: str,
    participant: str | None = None,
) -> str:
    """Register a Browser-Use Cloud scroll session with Convex.

    Standalone — no run_id. Used by `*_scroll.py` so the dashboard can render
    the live iframe in the matching platform slot. `participant` (when set)
    scopes the session to a specific iMessage conversation tab.
    """
    client = get_client()
    args: dict[str, Any] = {
        "platform": platform,
        "query": query,
        "liveUrl": live_url,
        "cloudSessionId": cloud_session_id,
    }
    if participant:
        args["participant"] = participant
    return await _run(client.mutation, "sessions:startCloud", args)


async def stop_by_participant(participant: str) -> dict[str, Any]:
    """Mark every running session for `participant` complete. Returns
    `{stopped: int, cloudSessionIds: list[str]}`."""
    client = get_client()
    return await _run(client.mutation, "sessions:stopByParticipant", {
        "participant": participant,
    })


async def finish_session(
    session_id: str, status: str, error: str | None = None
) -> None:
    client = get_client()
    args: dict[str, Any] = {"sessionId": session_id, "status": status}
    if error:
        args["error"] = error
    await _run(client.mutation, "sessions:finish", args)


async def update_session_query(session_id: str, query: str) -> None:
    """Update the live `query` text on a running scraperSessions row.

    Used when a supervised agent's task is redirected, so the dashboard
    label reflects the new task.
    """
    client = get_client()
    await _run(client.mutation, "sessions:updateQuery", {
        "sessionId": session_id,
        "query": query,
    })


async def patch_supervised(
    session_id: str,
    *,
    energy: float | None = None,
    restart_count: int | None = None,
    last_diagnosis: str | None = None,
) -> None:
    """Patch self-healing fields on a supervised session row."""
    args: dict[str, Any] = {"sessionId": session_id}
    if energy is not None:
        args["energy"] = energy
    if restart_count is not None:
        args["restartCount"] = restart_count
    if last_diagnosis is not None:
        args["lastDiagnosis"] = last_diagnosis
    if len(args) == 1:
        return  # nothing to patch
    client = get_client()
    await _run(client.mutation, "sessions:patchSupervised", args)


async def active_browser_count() -> int:
    client = get_client()
    raw = await _run(client.query, "sessions:activeBrowserCount", {})
    # Convex numbers can come back as floats over the wire.
    return int(raw)


# ── steps ─────────────────────────────────────────────────────────────────────


async def add_step(
    *,
    session_id: str,
    run_id: str,
    kind: str,
    url: str | None = None,
    title: str | None = None,
    text: str | None = None,
    screenshot: str | None = None,
) -> None:
    client = get_client()
    args: dict[str, Any] = {"sessionId": session_id, "runId": run_id, "kind": kind}
    if url:
        args["url"] = url
    if title:
        args["title"] = title
    if text:
        args["text"] = text
    if screenshot:
        args["screenshot"] = screenshot
    await _run(client.mutation, "steps:add", args)


# ── mentions ──────────────────────────────────────────────────────────────────


async def add_mention(*, session_id: str, run_id: str, mention: dict[str, Any]) -> None:
    client = get_client()
    payload = {"sessionId": session_id, "runId": run_id, **mention}
    await _run(client.mutation, "mentions:add", payload)


# ── screenshot upload ─────────────────────────────────────────────────────────


async def upload_screenshot(png_bytes: bytes) -> str | None:
    """Upload a PNG to Convex storage. Returns storageId or None on failure."""
    client = get_client()
    try:
        upload_url = await _run(
            client.mutation, "screenshots:generateUploadUrl", {}
        )
    except Exception as exc:
        logger.warning("convex screenshot upload-url failed: %s", exc)
        return None

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            res = await http.post(
                upload_url,
                content=png_bytes,
                headers={"Content-Type": "image/png"},
            )
            res.raise_for_status()
            return res.json().get("storageId")
    except Exception as exc:
        logger.warning("convex screenshot PUT failed: %s", exc)
        return None
