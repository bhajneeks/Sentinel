"""Social-pulse subagent — fan out the four Browser-Use scrapers in parallel.

Two entry points:

  - `gather_social_pulse(topic, platforms, ...)` — runs the chosen scrapers
    concurrently and returns a structured dict
    `{platform: {items, raw, success, ...}}`. Used as the optional 4th
    subagent inside `campaign.run_campaign_pipeline`.

  - `social_insights(topic, ...)` — runs the pulse, then asks the LLM for a
    short bulleted "what's happening right now" summary. Used by
    `POST /api/social-insights` for the no-campaign fast path.

Each platform run takes 30–90s on Browser-Use cloud. Defaults pull only 3
items per platform to keep the call bounded. Profile IDs come from env vars
(see `PLATFORMS` below) so server deployments can ship the right cookie
profile per platform without per-call config.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

import agent
import linkedin_scroll
import reddit_scroll
import tiktok_scroll
import twitter_scroll

logger = logging.getLogger("uvicorn.error")

# Platform → async scrape fn. Each fn signature: (query, *, top_n, scrolls,
# profile_id, ...) -> {"platform","query","items","raw","success"}.
ScrapeFn = Callable[..., Awaitable[dict[str, Any]]]

PLATFORMS: dict[str, ScrapeFn] = {
    "tiktok": tiktok_scroll.scrape,
    "twitter": twitter_scroll.scrape,
    "reddit": reddit_scroll.scrape,
    "linkedin": linkedin_scroll.scrape,
}

DEFAULT_PLATFORMS: tuple[str, ...] = ("twitter", "reddit", "linkedin")


def _normalize_platforms(platforms: list[str] | tuple[str, ...] | None) -> list[str]:
    if not platforms:
        return list(DEFAULT_PLATFORMS)
    cleaned: list[str] = []
    for p in platforms:
        key = p.strip().lower()
        if key == "x":
            key = "twitter"
        if key in PLATFORMS and key not in cleaned:
            cleaned.append(key)
    return cleaned or list(DEFAULT_PLATFORMS)


async def _run_one(platform: str, query: str, *, top_n: int, scrolls: int) -> dict[str, Any]:
    fn = PLATFORMS[platform]
    try:
        if platform == "tiktok":
            # TikTok's scrape() doesn't take top_n (uses a different prompt shape).
            return await fn(query, scrolls=scrolls)
        return await fn(query, scrolls=scrolls, top_n=top_n)
    except Exception as exc:
        logger.warning("social_pulse %s failed: %s", platform, exc)
        return {
            "platform": platform,
            "query": query,
            "items": [],
            "raw": None,
            "success": False,
            "error": str(exc),
        }


async def gather_social_pulse(
    topic: str,
    *,
    platforms: list[str] | tuple[str, ...] | None = None,
    top_n: int = 3,
    scrolls: int = 8,
) -> dict[str, Any]:
    """Run the chosen scrapers in parallel. Returns:

        {
          "topic": str,
          "platforms": [...],
          "results": {platform: {...}},
          "items_total": int,
        }

    Per-platform exceptions are caught and surfaced as `{"error": "..."}`
    so one bad scraper doesn't sink the rest — same failure model as the
    Reacher subagents in `campaign.py`.
    """
    chosen = _normalize_platforms(platforms)
    coros = [_run_one(p, topic, top_n=top_n, scrolls=scrolls) for p in chosen]
    results = await asyncio.gather(*coros, return_exceptions=False)
    by_platform = {r["platform"]: r for r in results}
    items_total = sum(len(r.get("items") or []) for r in results)
    return {
        "topic": topic,
        "platforms": chosen,
        "results": by_platform,
        "items_total": items_total,
    }


# ----- insights summarizer ---------------------------------------------------

INSIGHTS_SYSTEM_PROMPT = """You are a social-listening analyst. The user gives
you a topic and a JSON snapshot of the most recent posts about that topic
across TikTok, X (Twitter), Reddit, and LinkedIn (any subset).

Produce a tight markdown report in this EXACT structure:

# {topic} — pulse

## Summary
<2-3 sentences on the dominant narrative right now>

## Key insights
- 3-5 bullets. Each bullet: ONE concrete observation, then a parenthetical
  citation pointing to a specific source post by handle/author + platform
  (e.g. "(@SamJWasserman on X)"). Pull only insights that are visibly
  supported by the data — do NOT invent stats or names.

## Notable posts
- 2-4 bullets. Each: "<Author> on <Platform>: <one-line gist>" with the
  permalink in markdown link form. Prefer the freshest items.

## Gaps
- 0-2 bullets noting platforms with no data, or thin signal.

HARD RULES:
- Never fabricate a handle, follower count, or stat. If a field is missing,
  omit it rather than guess.
- If a platform's `items` list is empty or `success` is false, mention that
  platform in `## Gaps`.
- Keep the whole report under ~250 words. Tight is the goal.
"""


def _slim_results_for_llm(pulse: dict[str, Any]) -> dict[str, Any]:
    """Compact view of the pulse: items + a small raw preview for debugging."""
    slim: dict[str, Any] = {"topic": pulse.get("topic"), "platforms": {}}
    for platform, res in (pulse.get("results") or {}).items():
        items = res.get("items") or []
        entry: dict[str, Any] = {
            "success": res.get("success"),
            "items": items,
        }
        if res.get("error"):
            entry["error"] = res["error"]
        # When the parser found nothing, keep a short raw preview so the
        # caller can see what the agent actually returned.
        if not items and res.get("raw"):
            raw = res["raw"]
            entry["raw_preview"] = raw[:600] + ("…" if len(raw) > 600 else "")
        slim["platforms"][platform] = entry
    return slim


async def summarize_insights(topic: str, pulse: dict[str, Any]) -> str:
    """LLM call: structured pulse → markdown insights report."""
    user_payload = {
        "topic": topic,
        "snapshot": _slim_results_for_llm(pulse),
    }
    return await agent.chat_completion(
        messages=[
            {"role": "system", "content": INSIGHTS_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, default=str)[:60_000]},
        ],
        max_tokens=900,
        temperature=0.5,
    )


async def social_insights(
    topic: str,
    *,
    platforms: list[str] | tuple[str, ...] | None = None,
    top_n: int = 3,
    scrolls: int = 8,
) -> dict[str, Any]:
    """One-shot endpoint helper: scrape + summarize.

    Returns:
        {
          "topic": str,
          "platforms": [...],
          "insights_markdown": str,
          "pulse": {raw gather_social_pulse output, minus per-item `raw` blobs},
        }
    """
    pulse = await gather_social_pulse(
        topic, platforms=platforms, top_n=top_n, scrolls=scrolls,
    )
    if not agent.is_configured():
        raise RuntimeError(
            "OPENAI_API_KEY is not set — cannot summarize social insights."
        )
    markdown = await summarize_insights(topic, pulse)
    return {
        "topic": topic,
        "platforms": pulse["platforms"],
        "insights_markdown": markdown,
        "pulse": _slim_results_for_llm(pulse),
    }


__all__ = [
    "DEFAULT_PLATFORMS",
    "PLATFORMS",
    "gather_social_pulse",
    "social_insights",
    "summarize_insights",
]
