"""Browser-Use Cloud agent that searches X (Twitter) and tracks the latest tweets.

Uses the shared 'social' Browser-Use profile (also used by tiktok_scroll.py
and reddit_scroll.py). X heavily gates /search behind login, so log in once:

    uv run python social_login.py

Or per-platform:

    uv run python twitter_scroll.py --login-only

Usage:
    uv run python twitter_scroll.py --query "openai"
    uv run python twitter_scroll.py --query "browser use" --scrolls 20 --top 5

Docs:
    https://docs.browser-use.com/cloud/llms.txt
    https://docs.browser-use.com/cloud/quickstart
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from typing import Any
from urllib.parse import quote

from browser_use_common import (
    DEFAULT_PROFILE_NAME,
    add_common_args,
    run_login_session,
    run_scrape,
    run_scrape_collect,
)

PROFILE_ENV_VAR = "BROWSER_USE_TWITTER_PROFILE_ID"

SYSTEM_PROMPT = (
    "You are a Twitter/X research agent. Track the latest updates on the "
    "user's topic and return the N MOST RECENT tweets you can find."
)


def build_task(scrolls: int, query: str, top_n: int) -> tuple[str, str]:
    start_url = (
        f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"
    )

    task = (
        f"{SYSTEM_PROMPT}\n\n"
        f"You are on the X (Twitter) search results page for the query: '{query}'.\n"
        "Make sure the 'Latest' tab is selected (NOT 'Top', 'People', 'Media', "
        "or 'Lists'). If it isn't, click the 'Latest' tab.\n\n"
        "HARD RULES:\n"
        "1. NEVER attempt to log in or sign up. If you see a login wall, try "
        "closing it (X / Escape) and continue. Do NOT fill any form.\n"
        "2. NEVER click 'Sign in with Google/Apple', 'Create account', or any "
        "auth button.\n"
        "3. If a 'Don't miss what's happening' / cookie banner / 'Open in app' "
        "modal appears, dismiss it (X, Escape, 'Not now', or click outside).\n"
        "4. Stay on the search results timeline — do NOT click into tweets, "
        "author profiles, or media unless a field is otherwise unreachable. "
        "If you do open a tweet, use the back button to return.\n"
        "5. Skip promoted/ad tweets and pinned tweets — only collect organic "
        "results from the Latest timeline.\n\n"
        f"MAIN LOOP — repeat up to {scrolls} times, OR until you have observed "
        f"at least {top_n} distinct tweets:\n"
        " 1. Dismiss any popup/modal that appeared.\n"
        " 2. For each visible tweet card, note: full text (expand 'Show more' "
        "if present), author display name, author @handle, relative timestamp "
        "(e.g. '12m', '2h'), and the tweet permalink (the href on the "
        "timestamp link, of the form /<handle>/status/<id>).\n"
        " 3. Press the End key, or scroll down by ~1200px, to load more "
        "tweets. Wait ~1.5 seconds for new cards to render.\n\n"
        f"WHEN DONE — return STRICTLY a JSON array of the {top_n} MOST RECENT "
        "tweets (sorted newest-first), each object with these keys:\n"
        '  {"text": str, "author": str, "handle": str, '
        '"posted": str, "url": str, "summary": str}\n'
        "`url` should be the absolute permalink (https://x.com/<handle>/status/<id>). "
        "`summary` should be a one-sentence gist of the tweet. Do not include "
        "any prose outside the JSON array."
    )
    return start_url, task


def _parse_json_array(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


async def scrape(
    query: str,
    *,
    scrolls: int = 10,
    top_n: int = 3,
    profile_id: str | None = None,
    profile_name: str = DEFAULT_PROFILE_NAME,
    no_profile: bool = False,
    llm: str = "browser-use-2.0",
) -> dict[str, Any]:
    """Library entrypoint. Returns {'platform','query','items','raw','success'}."""
    pid = profile_id or os.environ.get(PROFILE_ENV_VAR)
    start_url, task = build_task(scrolls, query, top_n)
    success, raw = await run_scrape_collect(
        start_url=start_url,
        task=task,
        llm=llm,
        profile_name=profile_name,
        profile_id=pid,
        no_profile=no_profile,
    )
    return {
        "platform": "twitter",
        "query": query,
        "items": _parse_json_array(raw),
        "raw": raw,
        "success": success,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query", type=str, required=False, default=None,
        help="X/Twitter search query (required unless --login-only).",
    )
    parser.add_argument(
        "--scrolls", type=int, default=10,
        help="Max number of scroll steps on the Latest timeline (default: 10).",
    )
    parser.add_argument(
        "--top", type=int, default=3,
        help="Number of most-recent tweets to return (default: 3).",
    )
    add_common_args(parser)
    args = parser.parse_args()

    if args.login_only:
        if args.no_profile:
            raise SystemExit("--login-only requires a profile; drop --no-profile.")
        asyncio.run(run_login_session(
            start_url="https://x.com/i/flow/login",
            profile_name=args.profile,
            no_open=args.no_open,
            instructions=">>> Log into X in the live-preview window.",
        ))
        return

    if not args.query:
        parser.error("--query is required (unless using --login-only).")

    start_url, task = build_task(args.scrolls, args.query, args.top)
    asyncio.run(run_scrape(
        start_url=start_url,
        task=task,
        llm=args.llm,
        no_open=args.no_open,
        profile_name=args.profile,
        profile_id=args.profile_id,
        no_profile=args.no_profile,
        banner_extra={"Query:": args.query, "Top N:": str(args.top)},
        convex_platform="x",
        convex_query=args.query,
    ))


if __name__ == "__main__":
    main()
