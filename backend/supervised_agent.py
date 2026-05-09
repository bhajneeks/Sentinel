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
import json
import logging
import os
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
HARVEST_INTERVAL_S = 45.0
ENERGY_MAX = 100.0
ENERGY_TICK_DECAY = 4.0          # baseline drain per harvest tick
ENERGY_NO_YIELD_DRAIN = 6.0      # extra drain when a tick yields zero new approved FOUND
ENERGY_BAD_URL_DRAIN = 35.0      # captcha / login / blocked → fast drain to 0
ENERGY_BAD_EVAL_DRAIN = 12.0     # negative evaluation_previous_goal text
ENERGY_LOOP_DRAIN = 8.0          # repeating same next_goal
ENERGY_REFILL_ON_HIT = ENERGY_MAX  # full refill on judge-approved FOUND
MAX_RESTARTS = 3                 # before close


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
    # Harvester state — pulled from get_task() polls.
    run_id: str | None = None
    company: str = ""
    last_step_seen: int = 0
    seen_post_urls: set[str] = field(default_factory=set)
    rejected_post_urls: set[str] = field(default_factory=set)
    harvest_task: asyncio.Task[None] | None = None
    # Self-healing energy state.
    energy: float = ENERGY_MAX
    restart_count: int = 0
    recent_next_goals: list[str] = field(default_factory=list)
    last_energy_write_at: float = 0.0

    def touch(self) -> None:
        self.last_active_at = time.time()


# ── Harvester ─────────────────────────────────────────────────────────────────


# `FOUND | <url> | <author> | <summary>` — one match per line.
_FOUND_RE = re.compile(
    r"^\s*FOUND\s*\|\s*(?P<url>\S+?)\s*\|\s*(?P<author>[^|]+?)\s*\|\s*(?P<summary>.+?)\s*$",
    re.MULTILINE,
)


def _extract_found_lines(text: str) -> list[tuple[str, str, str]]:
    if not text:
        return []
    return [
        (m["url"].strip(), m["author"].strip(), m["summary"].strip())
        for m in _FOUND_RE.finditer(text)
    ]


def _is_relevant(haystack: str, company: str) -> bool:
    """Cheap keyword guard. Drops obviously-off posts that the agent
    accidentally FOUND'd (e.g. when a feed pivots away from the topic)."""
    if not company:
        return True
    return company.lower() in haystack.lower()


_JUDGE_SYSTEM = (
    "You are filtering social media posts for QUALITY + RELEVANCE. You are "
    "extremely strict. Most posts SHOULD be rejected. Respond with valid "
    "JSON only — no prose, no markdown."
)


def _judge_prompt(company: str, platform: str, url: str, author: str, raw_summary: str) -> str:
    return (
        f"Company being tracked: {company}\n"
        f"Platform: {platform}\n"
        f"Post URL: {url}\n"
        f"Author: {author}\n"
        f"Watcher agent summary: {raw_summary}\n\n"
        "Decide:\n"
        f"1) Is this post EXPLICITLY about {company}? The company (or its "
        "products / public people) must be the SUBJECT of the post — not "
        "a tangential mention, not an item in a 'top N AI companies' "
        "listicle, not a job/career post, not generic AI-industry chatter.\n"
        "2) Does it contain SUBSTANTIVE opinion, sentiment, criticism, "
        "praise, or concern from the author or commenters? Pure news "
        "announcements / press releases / promo / FYI links FAIL this test.\n\n"
        "If BOTH yes, respond:\n"
        '  {"relevant": true, "insight": "<one-sentence lowercase casual '
        'summary of WHAT PEOPLE THINK/FEEL about the company, 8-22 words. '
        'Examples: \'ppl genuinely worried about the new privacy policy\', '
        "'engineers raving about claude 4 — calling it the new SOTA', "
        "'lots of complaints about the pricing tier change'>\"}\n\n"
        "If EITHER no, respond:\n"
        '  {"relevant": false, "insight": null}\n\n'
        "JSON object only."
    )


async def _judge_post(
    company: str, platform: str, url: str, author: str, raw_summary: str
) -> dict[str, Any] | None:
    """LLM second-pass: relevance + opinion summary. Returns parsed JSON
    `{relevant: bool, insight: str | None}` or None on failure."""
    try:
        import agent  # local import — keeps supervised_agent usable standalone

        if not agent.is_configured():
            return None
        raw = await agent.chat_completion(
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": _judge_prompt(company, platform, url, author, raw_summary)},
            ],
            max_tokens=160,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        logger.warning("judge call failed: %s", exc)
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("judge returned non-JSON: %s", raw[:200])
        return None


async def _push_insight(
    participant: str,
    platform: str,
    insight: str,
    url: str,
) -> None:
    """Send Rachel-style fragments to iMessage AND echo into the dashboard
    chat thread so the user sees the insight in both places."""
    text = f"found this on {platform} || {insight} || {url}"
    fragments = [p.strip() for p in text.split("||") if p.strip()]

    # Lazy imports to avoid circular (main → tools → supervised_agent → main).
    try:
        import imessage
    except Exception:
        imessage = None  # type: ignore[assignment]
    try:
        import main as _main
    except Exception:
        _main = None  # type: ignore[assignment]

    for fragment in fragments:
        if imessage is not None:
            try:
                await imessage.send(participant, fragment, "iMessage")
            except Exception as exc:
                logger.warning("imessage send failed: %s", exc)

        if _main is not None:
            try:
                msg = _main.Message(
                    id=f"agent-{uuid.uuid4().hex[:12]}",
                    text=fragment,
                    participant=participant,
                    chatId=None,
                    chatKind="dm",
                    service="iMessage",
                    createdAt=datetime.now(timezone.utc),
                    direction="outbound",
                )
                _main.messages.append(msg)
                _main.broadcast(msg)
            except Exception as exc:
                logger.warning("dashboard echo failed: %s", exc)


_BAD_URL_KEYWORDS = (
    "captcha", "challenge", "verify", "login", "signin", "sign-in",
    "signup", "sign-up", "auth", "blocked", "denied", "checkpoint",
    "consent", "x.com/i/flow", "linkedin.com/checkpoint",
)
_BAD_EVAL_KEYWORDS = (
    "could not", "failed to", "blocked", "not visible", "unable to",
    "no way to", "stuck", "cannot proceed", "cannot find", "did not find",
    "couldn't find", "unable to dismiss", "unable to scroll",
)


def _step_signature(step: Any) -> str:
    val = getattr(step, "next_goal", None)
    return (val or "")[:160].lower().strip()


def _energy_delta_for_step(handle: AgentHandle, step: Any) -> float:
    """Diagnose a single step. Returns a negative delta to apply to energy."""
    delta = 0.0
    url = (getattr(step, "url", "") or "").lower()
    eval_prev = (getattr(step, "evaluation_previous_goal", "") or "").lower()

    if any(k in url for k in _BAD_URL_KEYWORDS):
        delta -= ENERGY_BAD_URL_DRAIN

    if any(k in eval_prev for k in _BAD_EVAL_KEYWORDS):
        delta -= ENERGY_BAD_EVAL_DRAIN

    sig = _step_signature(step)
    if sig and sig in handle.recent_next_goals:
        delta -= ENERGY_LOOP_DRAIN
    if sig:
        handle.recent_next_goals.append(sig)
        handle.recent_next_goals = handle.recent_next_goals[-5:]

    return delta


async def _maybe_write_energy(handle: AgentHandle) -> None:
    """Debounced Convex energy write — at most once per ~10s per handle."""
    now = time.time()
    if now - handle.last_energy_write_at < 10:
        return
    handle.last_energy_write_at = now
    try:
        await cx.patch_supervised(handle.convex_session_id, energy=handle.energy)
    except Exception as exc:
        logger.debug("energy write failed: %s", exc)


_DIAGNOSE_SYSTEM = (
    "You are diagnosing a stuck browser agent. Read the recent step trace "
    "AND the supervisor's prior decisions on this same session. Identify "
    "the primary cause and any pattern across prior revives. Respond with "
    "valid JSON only."
)
_PLAN_SYSTEM = (
    "You are planning a recovery for a stuck browser agent. Given the "
    "diagnosis and prior failed strategies, decide whether to attempt "
    "another revive or give up. If continuing, propose a NEW task that "
    "addresses the diagnosis specifically — do not repeat strategies that "
    "failed before. Respond with valid JSON only."
)


def _format_trace(recent_steps: list[Any]) -> str:
    lines: list[str] = []
    for s in recent_steps:
        lines.append(
            f"step {getattr(s, 'number', '?')}: "
            f"url={(getattr(s, 'url', '') or '')[:140]} | "
            f"eval={(getattr(s, 'evaluation_previous_goal', '') or '')[:200]} | "
            f"next={(getattr(s, 'next_goal', '') or '')[:200]}"
        )
    return "\n".join(lines) or "(no recent steps recorded)"


def _format_history(events: list[dict[str, Any]]) -> str:
    """Compact reverse-chronological list of prior supervisor decisions."""
    if not events:
        return "(no prior events for this session)"
    lines: list[str] = []
    # events come in desc order; format newest-first but readable
    for e in events[:10]:
        kind = e.get("kind", "?")
        diagnosis = (e.get("diagnosis") or "").strip()
        plan = (e.get("plan") or "").strip()
        rc = e.get("restartCount")
        bits = [f"[{kind}"]
        if rc is not None:
            bits.append(f" r{rc}")
        bits.append("]")
        if diagnosis:
            bits.append(f" diagnosis: {diagnosis[:200]}")
        if plan:
            bits.append(f" plan: {plan[:200]}")
        lines.append("".join(bits))
    return "\n".join(lines)


def _diagnose_prompt(
    handle: AgentHandle,
    recent_steps: list[Any],
    prior_events: list[dict[str, Any]],
) -> str:
    return (
        f"Platform: {handle.platform}\n"
        f"Company: {handle.company}\n"
        f"Energy: {handle.energy:.0f} / {ENERGY_MAX:.0f}\n"
        f"Restart attempt #{handle.restart_count + 1} of {MAX_RESTARTS}.\n\n"
        f"Current task (first 700 chars):\n{handle.current_task_text[:700]}\n\n"
        f"Recent step trace (oldest first):\n{_format_trace(recent_steps)}\n\n"
        f"Prior supervisor decisions on THIS session (newest first):\n"
        f"{_format_history(prior_events)}\n\n"
        "Respond with valid JSON:\n"
        '  {\n'
        '    "primary_cause": "<the main reason the agent stalled — e.g.'
        ' \'login wall on linkedin\', \'tiktok comments lazy-load timeout\','
        ' \'agent looping on dismiss-popup with no progress\'>",\n'
        '    "evidence": "<one short sentence quoting concrete signals'
        " from the trace (urls, eval text, repeated next_goals)>\",\n"
        '    "pattern": "<if this matches a prior revive\'s diagnosis,'
        " name the pattern (e.g. \\\"same captcha as r1\\\"); else"
        " null>\",\n"
        '    "is_recoverable": true | false\n'
        '  }\n'
        "JSON object only."
    )


def _plan_prompt(
    handle: AgentHandle,
    diagnosis: dict[str, Any],
    prior_events: list[dict[str, Any]],
) -> str:
    prior_strategies = []
    for e in prior_events[:5]:
        if e.get("kind") == "revive" and e.get("plan"):
            prior_strategies.append(f"- {e.get('plan', '')[:240]}")
    prior_block = (
        "\n".join(prior_strategies) if prior_strategies else "(no prior revive strategies)"
    )
    return (
        f"Platform: {handle.platform}\n"
        f"Company: {handle.company}\n"
        f"Restart attempt #{handle.restart_count + 1} of {MAX_RESTARTS} "
        f"(if you give_up, the session will close immediately).\n\n"
        f"Diagnosis from step 1:\n{json.dumps(diagnosis, indent=2)}\n\n"
        f"Strategies tried in prior revives on this session:\n{prior_block}\n\n"
        f"Current task (first 600 chars):\n{handle.current_task_text[:600]}\n\n"
        "Decide:\n"
        " - If diagnosis.is_recoverable is false OR the pattern field shows "
        "we are repeating a failed strategy, set decide=\"give_up\".\n"
        " - Otherwise, set decide=\"revive\" and author a NEW task that "
        "directly addresses primary_cause. Do NOT repeat any of the prior "
        "strategies above verbatim. Vary the approach: different start URL, "
        "different scroll mechanism, different popup-dismissal strategy, or "
        "different content target on the same platform.\n\n"
        "Constraints on new_task (only required when decide=\"revive\"):\n"
        f" - Continues monitoring '{handle.company}' on {handle.platform}.\n"
        " - PRESERVES the FOUND emit format:\n"
        "       FOUND | <full_post_permalink_url> | <author> | <one-sentence opinion summary>\n"
        " - Keeps the quality bar: only opinion-bearing posts EXPLICITLY about "
        "the company.\n\n"
        "Respond with valid JSON:\n"
        '  {\n'
        '    "decide": "revive" | "give_up",\n'
        '    "reasoning": "<one-sentence justification, naming the strategy '
        'change>",\n'
        '    "strategy_name": "<short label for the strategy, e.g. \\"hashtag-feed-pivot\\", \\"reload-and-wait-longer\\", \\"comments-only-skim\\">",\n'
        '    "new_task": "<full new task instruction, plain text — only required when decide=\\"revive\\">"\n'
        '  }\n'
        "JSON object only."
    )


async def _agent_chat_json(system: str, user: str, max_tokens: int = 700) -> dict[str, Any] | None:
    try:
        import agent
        if not agent.is_configured():
            return None
        raw = await agent.chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.4,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        logger.warning("agentic revive LLM call failed: %s", exc)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("agentic revive returned non-JSON: %s", raw[:200])
        return None


async def _diagnose_and_revive(handle: AgentHandle, recent_steps: list[Any]) -> bool:
    """Two-step agentic revive:
      step 1: diagnose primary cause + look for patterns vs prior revives
      step 2: plan — give_up or propose a NEW strategy not tried before
    Logs the full reasoning to supervisorEvents either way.
    Returns True if revive succeeded, False if we gave up (or LLM unavailable).
    """
    # Hard cap before any LLM work.
    if handle.restart_count >= MAX_RESTARTS:
        await _give_up(handle, reason="max restarts exhausted")
        return False

    # Pull prior events on this session — what's the agent already learned?
    prior_events = await cx.supervisor_events_for_session(
        handle.convex_session_id, limit=25,
    )

    # ── step 1: diagnose ────────────────────────────────────────────────
    diagnosis = await _agent_chat_json(
        _DIAGNOSE_SYSTEM,
        _diagnose_prompt(handle, recent_steps, prior_events),
        max_tokens=400,
    )
    if not diagnosis:
        # If the LLM is unavailable, fall back to a single retry of the
        # current task with cleared loop state.
        diagnosis = {
            "primary_cause": "unknown — LLM unavailable",
            "evidence": "",
            "pattern": None,
            "is_recoverable": True,
        }

    # ── step 2: plan ─────────────────────────────────────────────────────
    plan = await _agent_chat_json(
        _PLAN_SYSTEM,
        _plan_prompt(handle, diagnosis, prior_events),
        max_tokens=900,
    )
    if not plan:
        plan = {
            "decide": "revive",
            "reasoning": "LLM unavailable — retrying current task with cleared loop state",
            "strategy_name": "fallback-retry",
            "new_task": handle.current_task_text,
        }

    decide = (plan.get("decide") or "").strip().lower()
    new_task = (plan.get("new_task") or "").strip() or handle.current_task_text
    diagnosis_text = diagnosis.get("primary_cause") or "stuck"
    plan_text = (
        f"[{plan.get('strategy_name', 'unnamed')}] "
        f"{plan.get('reasoning', '')}".strip()
    )

    if decide == "give_up":
        await _give_up(handle, reason=diagnosis_text, plan=plan_text)
        return False

    logger.info(
        "supervised revive %s (attempt %d): %s — %s",
        handle.platform, handle.restart_count + 1, diagnosis_text, plan_text,
    )

    task_before = handle.current_task_text

    client = make_client()
    try:
        await client.tasks.update_task(handle.current_task_id, action="stop")
    except Exception as exc:
        logger.warning("revive stop-task failed: %s", exc)

    try:
        task_resp = await client.tasks.create_task(
            task=new_task,
            session_id=handle.cloud_session_id,
        )
    except Exception as exc:
        logger.warning("revive create_task failed for %s: %s", handle.platform, exc)
        await _give_up(handle, reason=f"revive create_task failed: {exc}")
        return False

    handle.current_task_id = task_resp.id
    handle.current_task_text = new_task
    handle.last_step_seen = 0
    handle.recent_next_goals.clear()
    handle.energy = ENERGY_MAX
    handle.restart_count += 1
    handle.last_energy_write_at = 0.0

    try:
        await cx.patch_supervised(
            handle.convex_session_id,
            energy=handle.energy,
            restart_count=handle.restart_count,
            last_diagnosis=diagnosis_text,
        )
        await cx.update_session_query(handle.convex_session_id, new_task[:200])
    except Exception as exc:
        logger.debug("revive convex patch failed: %s", exc)

    # Persistent log entry — captures full reasoning so the next revive
    # can see exactly what was tried and why.
    await cx.log_supervisor_event(
        kind="revive",
        participant=handle.participant,
        run_id=handle.run_id,
        session_id=handle.convex_session_id,
        platform=handle.platform,
        diagnosis=diagnosis_text,
        plan=plan_text,
        task_before=task_before,
        task_after=new_task,
        energy=handle.energy,
        restart_count=handle.restart_count,
    )

    return True


async def _give_up(
    handle: AgentHandle,
    *,
    reason: str,
    plan: str | None = None,
) -> None:
    logger.warning(
        "supervised %s giving up after %d restart(s): %s",
        handle.platform, handle.restart_count, reason,
    )
    try:
        await cx.patch_supervised(
            handle.convex_session_id,
            energy=0,
            last_diagnosis=reason[:500],
        )
    except Exception:
        pass
    await cx.log_supervisor_event(
        kind="give_up",
        participant=handle.participant,
        run_id=handle.run_id,
        session_id=handle.convex_session_id,
        platform=handle.platform,
        diagnosis=reason,
        plan=plan,
        energy=0,
        restart_count=handle.restart_count,
    )
    try:
        await _close_handle(handle)
    finally:
        _registry.get(handle.participant, {}).pop(handle.platform, None)  # type: ignore[arg-type]


async def _harvest_loop(handle: AgentHandle, interval: float = HARVEST_INTERVAL_S) -> None:
    """Per-handle background task. Polls get_task(), extracts FOUND lines,
    keyword-filters, writes Convex mentions. Idempotent via seen_post_urls.
    """
    client = make_client()
    while True:
        try:
            await asyncio.sleep(interval)
            try:
                task = await client.tasks.get_task(handle.current_task_id)
            except Exception as exc:
                logger.debug("harvest get_task failed for %s: %s", handle.platform, exc)
                continue

            steps = list(task.steps or [])
            new_steps = [s for s in steps if int(getattr(s, "number", 0) or 0) > handle.last_step_seen]

            # baseline tick decay (always)
            handle.energy = max(0.0, handle.energy - ENERGY_TICK_DECAY)

            # per-step diagnostics
            for step in new_steps:
                handle.energy = max(
                    0.0, handle.energy + _energy_delta_for_step(handle, step)
                )

            ticked_anything_useful = False  # set True if a judge-approved post lands

            if not new_steps:
                # no progress at all this tick → extra drain
                handle.energy = max(0.0, handle.energy - ENERGY_NO_YIELD_DRAIN)
                await _maybe_write_energy(handle)
                if handle.energy <= 0:
                    revived = await _diagnose_and_revive(handle, steps[-5:])
                    if not revived:
                        return
                continue

            for step in new_steps:
                blob_parts: list[str] = []
                for attr in ("memory", "next_goal", "evaluation_previous_goal"):
                    val = getattr(step, attr, None)
                    if isinstance(val, str) and val:
                        blob_parts.append(val)
                blob = "\n".join(blob_parts)

                for url, author, raw_summary in _extract_found_lines(blob):
                    if url in handle.seen_post_urls or url in handle.rejected_post_urls:
                        continue
                    # Cheap guard first — drops obvious garbage before we
                    # pay for the LLM judge call.
                    if not _is_relevant(f"{url} {raw_summary}", handle.company):
                        handle.rejected_post_urls.add(url)
                        logger.debug(
                            "harvest keyword-dropped: %s", url,
                        )
                        continue

                    # LLM judge — verifies the post is actually about the
                    # company AND opinion-bearing, and rewrites the summary
                    # into a Rachel-style insight.
                    verdict = await _judge_post(
                        handle.company, handle.platform, url, author, raw_summary,
                    )
                    if not verdict or not verdict.get("relevant"):
                        handle.rejected_post_urls.add(url)
                        logger.info(
                            "harvest judge-rejected %s: %s", handle.platform, url,
                        )
                        continue

                    insight = (verdict.get("insight") or "").strip()
                    if not insight:
                        handle.rejected_post_urls.add(url)
                        continue

                    handle.seen_post_urls.add(url)
                    ticked_anything_useful = True
                    handle.energy = ENERGY_REFILL_ON_HIT  # full reset on a real hit

                    # Persistent log: real win — refilled energy + writing to mentions.
                    await cx.log_supervisor_event(
                        kind="hit",
                        participant=handle.participant,
                        run_id=handle.run_id,
                        session_id=handle.convex_session_id,
                        platform=handle.platform,
                        diagnosis=f"insight: {insight}"[:500],
                        energy=handle.energy,
                        restart_count=handle.restart_count,
                    )

                    if handle.run_id:
                        try:
                            await cx.add_mention(
                                session_id=handle.convex_session_id,
                                run_id=handle.run_id,
                                mention={
                                    "platform": handle.platform,
                                    "postId": url,
                                    "postUrl": url,
                                    "postText": insight,
                                    "authorHandle": author,
                                    "authorDisplayName": author,
                                    "matchedTerms": [handle.company],
                                },
                            )
                            logger.info(
                                "harvested %s insight: %s", handle.platform, insight,
                            )
                        except Exception as exc:
                            logger.warning("add_mention failed: %s", exc)

                    # Push to iMessage + echo to dashboard chat.
                    await _push_insight(
                        handle.participant, handle.platform, insight, url,
                    )

            handle.last_step_seen = max(
                int(getattr(s, "number", 0) or 0) for s in new_steps
            )

            if not ticked_anything_useful:
                # steps progressed but nothing made it through the judge →
                # extra drain on top of step-level signals
                handle.energy = max(0.0, handle.energy - ENERGY_NO_YIELD_DRAIN)

            await _maybe_write_energy(handle)

            if handle.energy <= 0:
                revived = await _diagnose_and_revive(handle, steps[-5:])
                if not revived:
                    return
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("harvest loop error for %s: %s", handle.platform, exc)


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
    "5. You MAY click into a post's detail page to read its full text + "
    "top comments. After reading, press the browser BACK button to return "
    "to the search results feed. Do NOT navigate to author profiles, "
    "company pages, hashtag pages, or 'Show more results' / pagination.\n"
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
    "JSON output expected. Keep watching the feed indefinitely.\n\n"
    "QUALITY BAR — be RUTHLESSLY selective. Only emit a FOUND line when ALL "
    "of these are true:\n"
    " 1. The post is EXPLICITLY about the tracked company. The company "
    "name (or one of its products / public people) must be the SUBJECT of "
    "the post — not a tangential mention, not an item in a 'top 10' "
    "listicle, not a footnote in a roundup.\n"
    " 2. The post contains substantive OPINION, SENTIMENT, CRITICISM, "
    "PRAISE, or CONCERN — from the author or visible commenters. Pure "
    "news announcements, press-release reposts, ad copy, and bare links "
    "DO NOT QUALIFY.\n"
    " 3. It is NOT: a job listing, a hiring/career-advice post, a course "
    "ad, a giveaway, a 'just learned about X' beginner question, or a "
    "promo for an unrelated product that happens to mention the company.\n"
    " 4. You can read the full post text (if it's truncated and you "
    "can't expand it inline, SKIP it — do not navigate away).\n"
    "When in doubt, SKIP. Quality > quantity. It is much better to FOUND "
    "zero posts in a scroll cycle than to surface noise.\n\n"
    "FOUND FORMAT — append EXACTLY ONE LINE per qualifying post to your "
    "memory (no surrounding prose, no quotes, no markdown):\n"
    "     FOUND | <full_post_permalink_url> | <author_handle_or_display_name> | <one-sentence opinion-focused summary>\n"
    "   Use the literal `|` as separator. The url MUST be the absolute "
    "permalink to the individual post, never the search page. The summary "
    "should describe what people THINK or FEEL (e.g. 'commenters worried "
    "about the new privacy policy', 'OP frustrated with rate limits', "
    "'praising the new feature'), NOT what happened ('the company "
    "released X'). Skip posts you have already FOUND'd in a prior step.\n\n"
    "LOOP:\n"
    " - Periodically scroll to surface new posts.\n"
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
    extra = (
        "BEFORE EMITTING FOUND for any LinkedIn post, do these two things:\n"
        " - Click the inline 'see more' link on the post body so the full "
        "text is visible. If you cannot expand it, SKIP this post.\n"
        " - Click 'show more comments' or scroll the comment section to "
        "read the TOP 3-5 COMMENTS. Comments are where opinion lives on "
        "LinkedIn — the post itself is often a press release or self-promo, "
        "but commenters are honest. If there are zero comments OR the "
        "comments are pure congratulations / hashtags / no substance, SKIP.\n"
        "The FOUND summary must capture WHAT THE COMMENTERS THINK "
        "(or, if the post body itself contains a strong personal take, "
        "what the AUTHOR thinks). Avoid generic 'OP shares news about X'.\n\n"
        "DUPLICATE GUARD — before writing a new FOUND line, scan your own "
        "memory for any prior FOUND line with the same post URL. If you "
        "find one, SILENTLY SKIP. Never emit the same URL twice.\n"
    )
    body = (
        f"You are a LinkedIn research agent monitoring posts about '{company}'.\n"
        "Make sure the 'Posts' tab is selected and results are sorted by 'Latest'.\n\n"
        f"{_LINKEDIN_HARD_RULES}\n"
        f"{extra}"
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
    extra = (
        "TIKTOK SPECIAL RULES — captions alone are weak signal. You CANNOT "
        "watch the video itself, so YOU MUST READ THE COMMENTS to gauge "
        "sentiment. For each video you consider:\n"
        " - Read the full caption (expand if truncated).\n"
        " - Locate the comments panel (right side on desktop, bottom sheet "
        "on mobile). If the panel is collapsed, click the comment-bubble "
        "icon to open it. Scroll inside the comment panel to load more.\n"
        " - Read the TOP 5-10 COMMENTS (sorted by 'Most relevant' or "
        "'Top'). These carry the actual sentiment about the video and the "
        "company.\n"
        " - If the video has zero comments OR the comments are pure emoji "
        "/ '' / unrelated spam, SKIP this video.\n"
        "The FOUND summary MUST capture what the commenters say or feel "
        "about the company — e.g. 'commenters split on whether Anthropic's "
        "valuation is justified', 'top reply mocks the CEO's "
        "predictions about coding jobs'. NEVER summarize only the caption "
        "or the creator's framing.\n\n"
        "DUPLICATE GUARD — before writing a new FOUND line, scan your own "
        "memory for any prior FOUND line with the same video URL. If you "
        "find one, SILENTLY SKIP.\n"
    )
    body = (
        f"You are a TikTok research agent monitoring videos about '{company}'.\n"
        "Make sure the 'Videos' tab is selected. Click the first thumbnail to open the player, "
        "then advance with the Down arrow.\n\n"
        f"{_TIKTOK_HARD_RULES}\n"
        f"{extra}"
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
    run_id: str | None = None,
    start_harvester: bool = True,
) -> AgentHandle:
    """Open a keep_alive session, publish to Convex, start the initial task,
    and kick off the harvester loop. Pass start_harvester=False for
    diagnostic flows that want to poll raw step output themselves."""
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
        participant=participant,
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
        run_id=run_id,
        company=company,
    )
    # Seed Convex with the initial energy so the dashboard can render
    # the bar immediately (otherwise it'd remain null until the first
    # debounced write ~10s in).
    try:
        await cx.patch_supervised(handle.convex_session_id, energy=handle.energy)
    except Exception:
        pass

    # Persistent log: birth event.
    await cx.log_supervisor_event(
        kind="spawn",
        participant=participant,
        run_id=run_id,
        session_id=convex_id,
        platform=platform,
        diagnosis=f"spawned for company '{company}'",
        task_after=task_text,
        energy=handle.energy,
        restart_count=0,
    )

    if start_harvester:
        handle.harvest_task = asyncio.create_task(_harvest_loop(handle))
    return handle


# ── Public API ───────────────────────────────────────────────────────────────


async def spawn_company_agents(
    participant: str,
    company: str,
    *,
    run_id: str | None = None,
    overrides: dict[str, str] | None = None,
    start_harvester: bool = True,
) -> dict[str, str]:
    """Open one supervised session per platform for `company`.

    Honours the BROWSER_CONCURRENCY_CAP via convex_client.active_browser_count():
    skips platforms over cap and reports the skip in the result.

    `run_id` is required for harvested mentions to land in the Convex
    `mentions` table (which has a required runId field). Without it the
    harvester still runs but can't write mentions.

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
            handle = await _start_one(
                participant, p, company,
                override_task=overrides.get(p),
                run_id=run_id,
                start_harvester=start_harvester,
            )
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
    # New task → step numbers reset. Keep seen_post_urls so we don't
    # re-emit the same post under the redirected angle.
    handle.last_step_seen = 0

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
    *,
    run_id: str | None = None,
) -> str:
    """Add an additional session on `platform` (becomes a dashboard orbital)."""
    if platform not in PLATFORMS:
        return f"unknown platform: {platform}"

    headroom = max(0, cx.BROWSER_CONCURRENCY_CAP - await cx.active_browser_count())
    if headroom <= 0:
        return f"skipped: cap reached ({cx.BROWSER_CONCURRENCY_CAP})"

    # Inherit company from the existing slot. Falls back to the slot's
    # raw query text — if neither, harvested posts won't filter cleanly.
    slot = _registry.get(participant, {}).get(platform)  # type: ignore[arg-type]
    company = slot.company if slot else "unknown"

    try:
        handle = await _start_one(
            participant, platform, company,
            override_task=task,  # type: ignore[arg-type]
            run_id=run_id or (slot.run_id if slot else None),
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
    """Close every supervised handle for `participant`. Returns count closed.

    This handles in-memory handles AND any rows in Convex (which may belong
    to a previous backend process that has since restarted). The BU cloud
    sessions are stopped via the SDK using the cloudSessionIds Convex
    returns.
    """
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

    # Also stop any rows tracked in Convex that this process didn't spawn
    # (e.g. left behind by a prior backend process).
    try:
        result = await cx.stop_by_participant(participant)
    except Exception as exc:
        logger.warning("stop_by_participant convex call failed: %s", exc)
        return closed

    cloud_ids: list[str] = result.get("cloudSessionIds") or []
    if cloud_ids:
        client = make_client()
        for cs_id in cloud_ids:
            try:
                await client.sessions.update_session(cs_id, action="stop")
                closed += 1
            except Exception as exc:
                logger.warning("BU stop failed for %s: %s", cs_id, exc)
    return closed


async def _close_handle(handle: AgentHandle) -> None:
    if handle.harvest_task and not handle.harvest_task.done():
        handle.harvest_task.cancel()
        try:
            await handle.harvest_task
        except (asyncio.CancelledError, Exception):
            pass
    client = make_client()
    try:
        await client.sessions.update_session(handle.cloud_session_id, action="stop")
    except Exception as exc:
        logger.warning("session stop failed: %s", exc)
    try:
        await cx.finish_session(handle.convex_session_id, "complete")
    except Exception as exc:
        logger.warning("convex finish failed: %s", exc)
    # Persistent log: closure event. Best-effort.
    try:
        await cx.log_supervisor_event(
            kind="close",
            participant=handle.participant,
            run_id=handle.run_id,
            session_id=handle.convex_session_id,
            platform=handle.platform,
            energy=handle.energy,
            restart_count=handle.restart_count,
        )
    except Exception:
        pass


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
