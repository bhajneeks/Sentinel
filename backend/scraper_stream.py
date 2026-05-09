"""Wraps the existing scrapers with per-step Convex streaming.

For each platform:
- emit a `goto` step when we kick off
- (browser-use platforms only) emit a step per agent action with the URL,
  thinking, next goal, and a screenshot uploaded to Convex storage
- emit each parsed Mention as a row in the `mentions` table immediately
- mark the session complete or error when done
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from browser_use import Agent, Browser
from browser_use.agent.views import AgentOutput
from browser_use.browser.views import BrowserStateSummary
from browser_use.llm.models import ChatOpenAI

import convex_client as cx
from scraper import Mention, _MentionList

logger = logging.getLogger("uvicorn.error")

_AUTH_DIR = Path(__file__).parent / "auth"
_REDDIT_UA = "brand-monitor/0.1 by /u/placeholder"


Platform = Literal["reddit", "x", "linkedin"]


def _mention_to_payload(m: Mention) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "platform": m.platform,
        "postId": m.post_id,
        "postUrl": m.post_url,
        "postText": m.post_text,
        "authorHandle": m.author_handle,
        "authorDisplayName": m.author_display_name,
        "matchedTerms": m.matched_terms,
    }
    if m.subreddit:
        payload["subreddit"] = m.subreddit
    if m.post_type:
        payload["postType"] = m.post_type
    if m.posted_at:
        ts = (
            m.posted_at
            if m.posted_at.tzinfo
            else m.posted_at.replace(tzinfo=timezone.utc)
        )
        payload["postedAt"] = int(ts.timestamp() * 1000)
    if m.likes is not None:
        payload["likes"] = m.likes
    if m.reposts is not None:
        payload["reposts"] = m.reposts
    if m.comments is not None:
        payload["comments"] = m.comments
    return payload


# ── Reddit (no browser) ───────────────────────────────────────────────────────


async def _stream_reddit(*, run_id: str, session_id: str, query: str) -> int:
    await cx.add_step(
        session_id=session_id,
        run_id=run_id,
        kind="goto",
        url=f"https://www.reddit.com/search.json?q={urllib.parse.quote_plus(query)}",
        text=f"searching reddit for '{query}'",
    )

    headers = {"User-Agent": _REDDIT_UA}
    found = 0
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        for is_comment in (False, True):
            params: dict[str, str | int] = {
                "q": query,
                "sort": "new",
                "t": "day",
                "limit": 50,
            }
            if is_comment:
                params["type"] = "comment"
            try:
                r = await client.get(
                    "https://www.reddit.com/search.json", params=params
                )
                r.raise_for_status()
                children = r.json().get("data", {}).get("children", [])
            except Exception as exc:
                await cx.add_step(
                    session_id=session_id,
                    run_id=run_id,
                    kind="error",
                    text=f"reddit fetch failed: {exc}",
                )
                continue

            for child in children:
                d = child.get("data", {})
                post_id: str = d.get("name", "")
                if not post_id:
                    continue

                permalink = d.get("permalink", "")
                created_utc = d.get("created_utc")
                posted_at = (
                    datetime.fromtimestamp(created_utc, tz=timezone.utc)
                    if created_utc
                    else None
                )

                if is_comment:
                    text = d.get("body") or ""
                    post_type: Literal["post", "comment"] = "comment"
                else:
                    title = d.get("title") or ""
                    selftext = d.get("selftext") or ""
                    text = f"{title}\n\n{selftext}".rstrip()
                    post_type = "post"

                if not text:
                    continue

                m = Mention(
                    platform="reddit",
                    post_id=post_id,
                    post_url=f"https://www.reddit.com{permalink}",
                    author_handle=d.get("author") or "",
                    author_display_name=d.get("author") or "",
                    post_text=text,
                    subreddit=d.get("subreddit"),
                    post_type=post_type,
                    posted_at=posted_at,
                    likes=d.get("score"),
                    reposts=None,
                    comments=d.get("num_comments"),
                    matched_terms=[query],
                )
                try:
                    await cx.add_mention(
                        session_id=session_id,
                        run_id=run_id,
                        mention=_mention_to_payload(m),
                    )
                    found += 1
                except Exception as exc:
                    logger.warning("reddit mention insert failed: %s", exc)

            await asyncio.sleep(1)  # be polite

    return found


# ── browser-use (X / LinkedIn) ────────────────────────────────────────────────


def _platform_url(platform: Platform, query: str) -> str:
    q = urllib.parse.quote_plus(query)
    if platform == "x":
        return f"https://x.com/search?q={q}&f=live"
    return f"https://www.linkedin.com/search/results/content/?keywords={q}&sortBy=date_posted"


async def _stream_browser(
    *,
    platform: Platform,
    run_id: str,
    session_id: str,
    query: str,
) -> int:
    url = _platform_url(platform, query)
    await cx.add_step(
        session_id=session_id, run_id=run_id, kind="goto", url=url,
        text=f"opening {platform} for '{query}'",
    )

    state_file = _AUTH_DIR / f"{platform}_state.json"
    prompt = (
        f"Navigate to {url}. "
        f"Extract all visible posts mentioning '{query}'. "
        "For each post collect: post_id (from the post URL), post_url, "
        "author_handle, author_display_name, post_text, posted_at (ISO 8601), "
        "likes, reposts, comments. "
        "Do NOT log in, click into individual posts, or engage with any "
        "content. If you encounter an auth wall, CAPTCHA, or any blocker, "
        "return an empty mentions list."
    )

    async def on_step(state: BrowserStateSummary, output: AgentOutput, n: int) -> None:
        screenshot_id: str | None = None
        try:
            shot = state.screenshot
            if shot:
                if isinstance(shot, str):
                    import base64

                    raw = base64.b64decode(shot.split(",", 1)[-1])
                else:
                    raw = bytes(shot)
                screenshot_id = await cx.upload_screenshot(raw)
        except Exception as exc:
            logger.debug("screenshot capture skipped: %s", exc)

        text_bits = [
            output.next_goal or "",
            output.thinking or "",
        ]
        text = " · ".join(b for b in text_bits if b).strip() or f"step {n}"

        try:
            await cx.add_step(
                session_id=session_id,
                run_id=run_id,
                kind="action",
                url=state.url,
                title=state.title,
                text=text[:2000],
                screenshot=screenshot_id,
            )
        except Exception as exc:
            logger.warning("step write failed: %s", exc)

    found = 0
    try:
        llm = ChatOpenAI(model="gpt-4o")
        browser = Browser(
            storage_state=str(state_file) if state_file.exists() else None,
            user_data_dir=None,
        )
        agent = Agent(
            task=prompt,
            llm=llm,
            browser=browser,
            output_model_schema=_MentionList,
            register_new_step_callback=on_step,
        )
        result = await asyncio.wait_for(agent.run(), timeout=180)
        raw = result.final_result() if hasattr(result, "final_result") else result
        if not isinstance(raw, _MentionList):
            await cx.add_step(
                session_id=session_id,
                run_id=run_id,
                kind="error",
                text=f"parse failure: unexpected result type {type(raw).__name__}",
            )
            return 0

        for m in raw.mentions:
            m.platform = platform
            try:
                await cx.add_mention(
                    session_id=session_id,
                    run_id=run_id,
                    mention=_mention_to_payload(m),
                )
                found += 1
            except Exception as exc:
                logger.warning("%s mention insert failed: %s", platform, exc)

    except asyncio.TimeoutError:
        await cx.add_step(
            session_id=session_id,
            run_id=run_id,
            kind="error",
            text="timeout after 180s",
        )
        raise
    except Exception as exc:
        msg = str(exc).lower()
        kind = "error"
        if any(k in msg for k in ("captcha", "auth", "login", "sign in")):
            await cx.add_step(
                session_id=session_id,
                run_id=run_id,
                kind=kind,
                text=f"auth/captcha wall: {exc}",
            )
        else:
            await cx.add_step(
                session_id=session_id, run_id=run_id, kind=kind, text=str(exc)
            )
        raise

    return found


# ── public entrypoint ─────────────────────────────────────────────────────────


async def run_scraper(
    *,
    run_id: str,
    session_id: str,
    platform: Platform,
    query: str,
) -> None:
    try:
        if platform == "reddit":
            count = await _stream_reddit(
                run_id=run_id, session_id=session_id, query=query
            )
        else:
            count = await _stream_browser(
                platform=platform,
                run_id=run_id,
                session_id=session_id,
                query=query,
            )
        await cx.add_step(
            session_id=session_id,
            run_id=run_id,
            kind="extract",
            text=f"finished: {count} mentions",
        )
        await cx.finish_session(session_id, "complete")
    except Exception as exc:
        logger.exception("scraper %s crashed", platform)
        await cx.finish_session(session_id, "error", str(exc))
