"""Long-lived, LLM-supervised Browser-Use Cloud agents (per-platform).

Unlike `*_scroll.py`, which fires one task and tears down the session,
supervised agents:
- Open sessions with `keep_alive=True` so they outlive their initial task.
- Accept new tasks ("redirects") via `update_task(stop)` + `create_task(session_id=...)`.
- Live in module-level registries keyed by participant.
- Are auto-closed by a 30-minute idle janitor.

Public surface used by `tools.py`:
  - `spawn_company_agents(participant, company, *, overrides=None)`
  - `screenshot(participant, instance)`
  - `redirect(participant, instance, task)`
  - `close(participant, instance)`
  - `spawn(participant, platform, task=None)`
  - `close_all_for_participant(participant)`
  - `janitor_tick()`

`instance` is either a platform name ("linkedin") for the slot session,
or `"<platform>@<n>"` for an orbit (`linkedin@2`, `linkedin@3`, ...).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

import convex_client as cx
from browser_use_common import ConvexPlatform, make_client

logger = logging.getLogger("supervised_agent")

PLATFORMS: tuple[ConvexPlatform, ...] = ("linkedin", "x", "reddit", "tiktok")

PROFILE_ENV_VAR_BY_PLATFORM: dict[ConvexPlatform, str] = {
    "linkedin": "BROWSER_USE_LINKEDIN_PROFILE_ID",
    "x": "BROWSER_USE_TWITTER_PROFILE_ID",
    "reddit": "BROWSER_USE_REDDIT_PROFILE_ID",
    "tiktok": "BROWSER_USE_TIKTOK_PROFILE_ID",
}

IDLE_CLOSE_AFTER_S = 30 * 60  # 30 minutes


@dataclass
class AgentHandle:
    participant: str
    platform: ConvexPlatform
    cloud_session_id: str
    convex_session_id: str
    current_task_id: str
    live_url: str
    started_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    current_task_text: str = ""

    def touch(self) -> None:
        self.last_active_at = time.time()


# Slot session per (participant, platform). One slot per platform.
_registry: dict[str, dict[ConvexPlatform, AgentHandle]] = {}
# Orbit sessions per (participant, platform). Ordered list (insertion order).
_orbits: dict[str, dict[ConvexPlatform, list[AgentHandle]]] = {}


# ── Default per-platform task templates ───────────────────────────────────────
#
# These are stripped-down versions of the `*_scroll.py:build_task` prompts:
# the JSON-array exit clause is removed and a "keep observing" trailer is
# added so the supervised agent stays on the feed instead of finalizing.

_LINKEDIN_HARD_RULES = (
    "HARD RULES:\n"
    "1. NEVER attempt to log in or sign up. The session is already authenticated.\n"
    "2. NEVER click 'Sign in with Google/Apple', 'Join now', or any auth button.\n"
    "3. Dismiss popups (X, Escape, 'Not now', or click outside).\n"
    "4. NEVER send a connection request, follow, like, comment, repost, or message.\n"
    "5. Stay on the search results feed. Click only inline 'see more' to expand a post in place.\n"
)

_X_HARD_RULES = (
    "HARD RULES:\n"
    "1. NEVER attempt to log in or sign up. If a login wall appears, close it (X / Escape).\n"
    "2. NEVER click 'Sign in with Google/Apple', 'Create account', or any auth button.\n"
    "3. Dismiss popups (X, Escape, 'Not now', or click outside).\n"
    "4. Stay on the search results timeline; do NOT click into individual tweets.\n"
    "5. Skip promoted/ad tweets and pinned tweets — observe organic results only.\n"
)

_REDDIT_HARD_RULES = (
    "HARD RULES:\n"
    "1. NEVER attempt to log in or sign up. Browse as a guest.\n"
    "2. NEVER click 'Continue with Google' or any auth button.\n"
    "3. Dismiss popups ('Continue in browser', NSFW warning, etc.).\n"
    "4. Stay on the search listing; do NOT click into individual posts.\n"
)

_TIKTOK_HARD_RULES = (
    "HARD RULES:\n"
    "1. NEVER attempt to log in, sign up, or fill any form.\n"
    "2. NEVER click 'Continue with Google', 'Use phone / email', or any auth button.\n"
    "3. NEVER engage with 'Verify it's you' / captcha challenges. Close the modal and continue.\n"
    "4. NEVER click on a creator profile, @handle, avatar, or user card.\n"
    "5. Dismiss any popup BEFORE every scroll.\n"
)

_OBSERVATION_TRAILER = (
    "\n\nThis is a SUPERVISED, OPEN-ENDED observation task. There is NO terminal "
    "JSON output expected. Keep watching the feed indefinitely:\n"
    " - Periodically scroll to surface new posts.\n"
    " - Note (in your memory / next-goal text) any new post about the topic that "
    "appears since the last step.\n"
    " - Stay on the listing; do not navigate away.\n"
    " - If the page errors out or hits an auth wall, reload the start URL.\n"
    "Continue until you receive a new task instruction or until the session is stopped."
)


def _linkedin_task(company: str) -> tuple[str, str]:
    q = urllib.parse.quote(company)
    start = (
        "https://www.linkedin.com/search/results/content/"
        f"?keywords={q}&sortBy=%22date_posted%22"
    )
    body = (
        f"You are a LinkedIn research agent monitoring posts about '{company}'.\n"
        "Make sure the 'Posts' tab is selected and results are sorted by 'Latest'.\n\n"
        f"{_LINKEDIN_HARD_RULES}"
        f"{_OBSERVATION_TRAILER}"
    )
    return start, body


def _x_task(company: str) -> tuple[str, str]:
    q = urllib.parse.quote(company)
    start = f"https://x.com/search?q={q}&src=typed_query&f=live"
    body = (
        f"You are an X (Twitter) research agent monitoring tweets about '{company}'.\n"
        "Make sure the 'Latest' tab is selected.\n\n"
        f"{_X_HARD_RULES}"
        f"{_OBSERVATION_TRAILER}"
    )
    return start, body


def _reddit_task(company: str) -> tuple[str, str]:
    q = urllib.parse.quote(company)
    start = f"https://www.reddit.com/search/?q={q}&sort=new&t=day"
    body = (
        f"You are a Reddit research agent monitoring posts about '{company}'.\n"
        "Sort by 'New' if available.\n\n"
        f"{_REDDIT_HARD_RULES}"
        f"{_OBSERVATION_TRAILER}"
    )
    return start, body


def _tiktok_task(company: str) -> tuple[str, str]:
    q = urllib.parse.quote(company)
    start = f"https://www.tiktok.com/search/video?q={q}"
    body = (
        f"You are a TikTok research agent monitoring videos about '{company}'.\n"
        "Make sure the 'Videos' tab is selected. Click the first thumbnail to open the player, "
        "then advance with the Down arrow.\n\n"
        f"{_TIKTOK_HARD_RULES}"
        f"{_OBSERVATION_TRAILER}"
    )
    return start, body


_DEFAULT_TASK_BUILDERS: dict[ConvexPlatform, Any] = {
    "linkedin": _linkedin_task,
    "x": _x_task,
    "reddit": _reddit_task,
    "tiktok": _tiktok_task,
}


# ── Internal helpers ──────────────────────────────────────────────────────────


def _resolve_profile_id(platform: ConvexPlatform) -> str | None:
    return os.environ.get(PROFILE_ENV_VAR_BY_PLATFORM[platform])


def _parse_instance(instance: str) -> tuple[ConvexPlatform, int]:
    """`linkedin` -> ('linkedin', 0).  `linkedin@2` -> ('linkedin', 1)."""
    if "@" in instance:
        platform, idx = instance.split("@", 1)
        return platform, max(int(idx) - 1, 0)  # @1 = slot, @2 = orbits[0]
    return instance, 0


def _resolve_handle(participant: str, instance: str) -> AgentHandle | None:
    platform, idx = _parse_instance(instance)
    if platform not in PLATFORMS:
        return None
    if idx == 0:
        return _registry.get(participant, {}).get(platform)  # type: ignore[arg-type]
    orbit_list = _orbits.get(participant, {}).get(platform, [])  # type: ignore[arg-type]
    return orbit_list[idx - 1] if 0 <= idx - 1 < len(orbit_list) else None


def _instance_id(participant: str, handle: AgentHandle) -> str:
    """Reverse lookup: which `linkedin` / `linkedin@2` is this handle?"""
    slot = _registry.get(participant, {}).get(handle.platform)
    if slot is handle:
        return handle.platform
    orbits = _orbits.get(participant, {}).get(handle.platform, [])
    for i, h in enumerate(orbits, start=2):
        if h is handle:
            return f"{handle.platform}@{i}"
    return handle.platform


async def _start_one(
    participant: str,
    platform: ConvexPlatform,
    company: str,
    *,
    override_task: str | None = None,
) -> AgentHandle:
    """Open a keep_alive session, publish to Convex, and start the initial task."""
    builder = _DEFAULT_TASK_BUILDERS[platform]
    start_url, default_task = builder(company)
    task_text = override_task or default_task

    client = make_client()
    profile_id = _resolve_profile_id(platform)

    session_kwargs: dict[str, Any] = {"start_url": start_url, "keep_alive": True}
    if profile_id:
        session_kwargs["profile_id"] = profile_id

    session = await client.sessions.create_session(**session_kwargs)
    if not session.live_url:
        raise RuntimeError(f"{platform}: session had no live_url")

    convex_id = await cx.start_cloud_session(
        platform=platform,
        query=company,
        live_url=session.live_url,
        cloud_session_id=session.id,
    )
    if not convex_id:
        # Convex isn't configured / publish failed — close the cloud session
        # so we don't leak a billed session no one can see.
        try:
            await client.sessions.update_session(session.id, action="stop")
        except Exception:
            pass
        raise RuntimeError(f"{platform}: convex publish failed")

    task_resp = await client.tasks.create_task(
        task=task_text,
        session_id=session.id,
        start_url=start_url,
    )

    handle = AgentHandle(
        participant=participant,
        platform=platform,
        cloud_session_id=session.id,
        convex_session_id=convex_id,
        current_task_id=task_resp.id,
        live_url=session.live_url,
        current_task_text=task_text,
    )
    return handle


# ── Public API ───────────────────────────────────────────────────────────────


async def spawn_company_agents(
    participant: str,
    company: str,
    *,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Open one supervised session per platform for `company`.

    Honours the BROWSER_CONCURRENCY_CAP via convex_client.active_browser_count():
    skips platforms over cap and reports the skip in the result.

    Returns: mapping platform -> "started" | "skipped: cap" | f"failed: {reason}".
    """
    # Close any prior run for this participant first to avoid leaks.
    await close_all_for_participant(participant)

    overrides = overrides or {}
    headroom = max(0, cx.BROWSER_CONCURRENCY_CAP - await cx.active_browser_count())

    # Decide which platforms get to spawn (in PLATFORMS order; cap-limited).
    chosen: list[ConvexPlatform] = []
    skipped: list[ConvexPlatform] = []
    for p in PLATFORMS:
        if len(chosen) < headroom:
            chosen.append(p)
        else:
            skipped.append(p)

    async def _try(p: ConvexPlatform) -> tuple[ConvexPlatform, str | AgentHandle]:
        try:
            handle = await _start_one(participant, p, company, override_task=overrides.get(p))
            return p, handle
        except Exception as exc:
            logger.warning("supervised spawn failed for %s: %s", p, exc)
            return p, f"failed: {exc}"

    results = await asyncio.gather(*[_try(p) for p in chosen])

    out: dict[str, str] = {}
    _registry.setdefault(participant, {})
    _orbits.setdefault(participant, {})
    for p, val in results:
        if isinstance(val, AgentHandle):
            _registry[participant][p] = val
            out[p] = "started"
        else:
            out[p] = val
    for p in skipped:
        out[p] = "skipped: cap"
    return out


async def screenshot(participant: str, instance: str) -> dict[str, Any]:
    """Return latest step screenshot URL + metadata for an instance."""
    handle = _resolve_handle(participant, instance)
    if handle is None:
        return {"error": f"no agent found for {instance}"}
    handle.touch()

    client = make_client()
    try:
        task = await client.tasks.get_task(handle.current_task_id)
        status_resp = await client.tasks.get_task_status(handle.current_task_id)
    except Exception as exc:
        return {"error": f"sdk: {exc}"}

    steps = list(task.steps or [])
    latest = steps[-1] if steps else None
    return {
        "instance_id": _instance_id(participant, handle),
        "platform": handle.platform,
        "task_status": status_resp.status if status_resp else None,
        "current_url": getattr(latest, "url", None),
        "step_index": getattr(latest, "number", None),
        "step_summary": getattr(latest, "next_goal", None),
        "screenshot_url": getattr(latest, "screenshot_url", None),
        "live_url": handle.live_url,
        "current_task": handle.current_task_text,
    }


async def redirect(participant: str, instance: str, task: str) -> str:
    """Stop the current task, queue a new one on the same session."""
    handle = _resolve_handle(participant, instance)
    if handle is None:
        return f"no agent found for {instance}"
    handle.touch()

    client = make_client()
    try:
        # Stop only the task — keep the session alive (keep_alive=True).
        await client.tasks.update_task(handle.current_task_id, action="stop")
    except Exception as exc:
        logger.warning("redirect stop-task failed: %s", exc)
        # Continue anyway; create_task on the same session should still queue.

    try:
        task_resp = await client.tasks.create_task(
            task=task,
            session_id=handle.cloud_session_id,
        )
    except Exception as exc:
        return f"redirect failed: {exc}"

    handle.current_task_id = task_resp.id
    handle.current_task_text = task

    try:
        await cx.update_session_query(handle.convex_session_id, task[:200])
    except Exception as exc:
        logger.warning("convex updateQuery failed: %s", exc)

    return f"redirected {_instance_id(participant, handle)} → new task queued"


async def close(participant: str, instance: str) -> str:
    """Stop the session, mark Convex complete, promote first orbit if any."""
    platform, idx = _parse_instance(instance)
    if platform not in PLATFORMS:
        return f"unknown platform: {platform}"

    handle = _resolve_handle(participant, instance)
    if handle is None:
        return f"no agent found for {instance}"

    await _close_handle(handle)

    if idx == 0:
        # Slot session — promote first orbit if any.
        slot = _registry.get(participant, {})
        slot.pop(platform, None)  # type: ignore[arg-type]
        orbits = _orbits.get(participant, {}).get(platform, [])  # type: ignore[arg-type]
        if orbits:
            promoted = orbits.pop(0)
            slot[platform] = promoted  # type: ignore[index]
            return (
                f"closed {platform}; promoted orbit to slot "
                f"({_instance_id(participant, promoted)})"
            )
        return f"closed {platform}"
    # Orbit close.
    orbits = _orbits.get(participant, {}).get(platform, [])  # type: ignore[arg-type]
    if 0 <= idx - 1 < len(orbits):
        orbits.pop(idx - 1)
    return f"closed {instance}"


async def spawn(
    participant: str,
    platform: str,
    task: str | None = None,
) -> str:
    """Add an additional session on `platform` (becomes a dashboard orbital)."""
    if platform not in PLATFORMS:
        return f"unknown platform: {platform}"

    headroom = max(0, cx.BROWSER_CONCURRENCY_CAP - await cx.active_browser_count())
    if headroom <= 0:
        return f"skipped: cap reached ({cx.BROWSER_CONCURRENCY_CAP})"

    # Use the existing slot's `query` as the company hint if no task given.
    slot = _registry.get(participant, {}).get(platform)  # type: ignore[arg-type]
    company = slot.current_task_text.split("'")[1] if (slot and "'" in slot.current_task_text) else "unknown"

    try:
        handle = await _start_one(
            participant, platform, company, override_task=task,  # type: ignore[arg-type]
        )
    except Exception as exc:
        return f"spawn failed: {exc}"

    _orbits.setdefault(participant, {}).setdefault(platform, []).append(handle)  # type: ignore[index]
    if not _registry.get(participant, {}).get(platform):  # type: ignore[arg-type]
        # No slot existed — promote this one to slot.
        _registry.setdefault(participant, {})[platform] = handle  # type: ignore[index]
        _orbits[participant][platform].pop()  # type: ignore[index]
        return f"spawned {platform} (filled empty slot)"
    return f"spawned {_instance_id(participant, handle)}"


async def close_all_for_participant(participant: str) -> int:
    """Close every supervised handle for `participant`. Returns count closed."""
    closed = 0
    handles_to_close: list[AgentHandle] = []
    for h in list(_registry.get(participant, {}).values()):
        handles_to_close.append(h)
    for orbit_list in _orbits.get(participant, {}).values():
        handles_to_close.extend(orbit_list)
    for h in handles_to_close:
        try:
            await _close_handle(h)
            closed += 1
        except Exception as exc:
            logger.warning("close_all: %s", exc)
    _registry.pop(participant, None)
    _orbits.pop(participant, None)
    return closed


async def _close_handle(handle: AgentHandle) -> None:
    client = make_client()
    try:
        await client.sessions.update_session(handle.cloud_session_id, action="stop")
    except Exception as exc:
        logger.warning("session stop failed: %s", exc)
    try:
        await cx.finish_session(handle.convex_session_id, "complete")
    except Exception as exc:
        logger.warning("convex finish failed: %s", exc)


# ── Janitor ───────────────────────────────────────────────────────────────────


async def janitor_tick() -> int:
    """Close handles idle for more than IDLE_CLOSE_AFTER_S. Returns count closed."""
    cutoff = time.time() - IDLE_CLOSE_AFTER_S
    stale: list[tuple[str, AgentHandle, str]] = []
    for participant, by_platform in _registry.items():
        for platform, handle in by_platform.items():
            if handle.last_active_at < cutoff:
                stale.append((participant, handle, platform))
    for participant, by_platform in _orbits.items():
        for platform, orbit_list in by_platform.items():
            for handle in orbit_list:
                if handle.last_active_at < cutoff:
                    stale.append((participant, handle, platform))

    for participant, handle, platform in stale:
        instance = _instance_id(participant, handle)
        try:
            await close(participant, instance)
            logger.info("janitor closed idle %s for %s", instance, participant)
        except Exception as exc:
            logger.warning("janitor close failed for %s: %s", instance, exc)
    return len(stale)


def list_active(participant: str) -> dict[str, list[str]]:
    """For debugging: which slots/orbits exist for this participant."""
    out: dict[str, list[str]] = {"slots": [], "orbits": []}
    for platform, handle in _registry.get(participant, {}).items():
        out["slots"].append(_instance_id(participant, handle))
    for platform, orbit_list in _orbits.get(participant, {}).items():
        for handle in orbit_list:
            out["orbits"].append(_instance_id(participant, handle))
    return out


__all__ = [
    "AgentHandle",
    "PLATFORMS",
    "close",
    "close_all_for_participant",
    "janitor_tick",
    "list_active",
    "redirect",
    "screenshot",
    "spawn",
    "spawn_company_agents",
]
