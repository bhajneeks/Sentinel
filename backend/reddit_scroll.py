"""Browser-Use Cloud agent that searches Reddit and tracks the latest 3 posts.

Uses the shared 'social' Browser-Use profile (also used by tiktok_scroll.py
and twitter_scroll.py). Log in once with:

    uv run python social_login.py

Or per-platform:

    uv run python reddit_scroll.py --login-only

Usage:
    uv run python reddit_scroll.py --query "openai"
    uv run python reddit_scroll.py --query "browser use" --scrolls 20

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
    parse_json_array,
    run_login_session,
    run_scrape,
    run_scrape_collect,
)

PROFILE_ENV_VAR = "BROWSER_USE_REDDIT_PROFILE_ID"

SYSTEM_PROMPT = (
    "You are a Reddit research agent. Track the latest updates on the user's "
    "topic and return the 3 MOST RECENT posts you can find."
)


def build_task(scrolls: int, query: str, top_n: int) -> tuple[str, str]:
    start_url = (
        f"https://www.reddit.com/search/?q={quote(query)}&sort=new&t=day"
    )

    task = (
        f"{SYSTEM_PROMPT}\n\n"
        f"You are on the Reddit search results page for the query: '{query}'.\n"
        "The results should already be sorted by NEW. If they are not, click "
        "the sort dropdown and choose 'New'.\n\n"
        "HARD RULES:\n"
        "1. NEVER attempt to log in or sign up. Browse as a guest.\n"
        "2. NEVER click 'Continue with Google' or any auth button.\n"
        "3. If a 'Log in to continue' / 'Open in app' / cookie banner / NSFW "
        "warning modal appears, dismiss it (X, Escape, 'Not now', 'Continue "
        "in browser', or click outside).\n"
        "4. Stay on the search results listing — do NOT click into individual "
        "posts unless you need a missing field (timestamp/author). If you do "
        "open a post, use the back button to return to the listing.\n"
        "5. If results look empty or filtered, fall back to "
        f"https://www.reddit.com/search/?q={quote(query)}&sort=new (no time filter).\n\n"
        f"MAIN LOOP — repeat up to {scrolls} times, OR until you have observed "
        f"at least {top_n} distinct posts:\n"
        " 1. Dismiss any popup/modal that appeared.\n"
        " 2. For each visible post tile, note: title, subreddit (r/...), "
        "author (u/...), how long ago it was posted (e.g. '2 hours ago'), "
        "and the post permalink (href on the title).\n"
        " 3. Press the End key, or scroll down by ~1000px, to reveal more "
        "posts. Wait ~1.5 seconds for new tiles to render.\n\n"
        f"WHEN DONE — return STRICTLY a JSON array of the {top_n} MOST RECENT "
        "posts (sorted newest-first), each object with these keys:\n"
        '  {"title": str, "subreddit": str, "author": str, '
        '"posted": str, "url": str, "summary": str}\n'
        "`summary` should be a one-sentence gist of the post (from the title "
        "+ any visible preview text). Do not include any prose outside the "
        "JSON array."
    )
    return start_url, task


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
        "platform": "reddit",
        "query": query,
        "items": parse_json_array(raw),
        "raw": raw,
        "success": success,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query", type=str, required=False, default=None,
        help="Reddit search query (required unless --login-only).",
    )
    parser.add_argument(
        "--scrolls", type=int, default=10,
        help="Max number of scroll steps on the results listing (default: 10).",
    )
    parser.add_argument(
        "--top", type=int, default=3,
        help="Number of most-recent posts to return (default: 3).",
    )
    add_common_args(parser)
    args = parser.parse_args()

    if args.login_only:
        if args.no_profile:
            raise SystemExit("--login-only requires a profile; drop --no-profile.")
        asyncio.run(run_login_session(
            start_url="https://www.reddit.com/login",
            profile_name=args.profile,
            no_open=args.no_open,
            instructions=">>> Log into Reddit in the live-preview window.",
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
    ))


if __name__ == "__main__":
    main()
