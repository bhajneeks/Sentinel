"""Browser-Use Cloud agent that searches LinkedIn and tracks the latest posts.

Uses the shared 'social' Browser-Use profile (also used by tiktok_scroll.py,
reddit_scroll.py, twitter_scroll.py). LinkedIn requires login for content
search, so log in once:

    uv run python social_login.py
    # then in the live preview, also navigate to https://www.linkedin.com/login

Or per-platform:

    uv run python linkedin_scroll.py --login-only

Usage:
    uv run python linkedin_scroll.py --query "openai"
    uv run python linkedin_scroll.py --query "browser use" --scrolls 20 --top 5

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

PROFILE_ENV_VAR = "BROWSER_USE_LINKEDIN_PROFILE_ID"

SYSTEM_PROMPT = (
    "You are a LinkedIn research agent. Track the latest updates on the "
    "user's topic and return the N MOST RECENT posts you can find."
)


def build_task(scrolls: int, query: str, top_n: int) -> tuple[str, str]:
    # LinkedIn content search, sorted by latest (`sortBy=date_posted`).
    start_url = (
        "https://www.linkedin.com/search/results/content/"
        f"?keywords={quote(query)}&sortBy=%22date_posted%22"
    )

    task = (
        f"{SYSTEM_PROMPT}\n\n"
        f"You are on the LinkedIn 'Posts' search results page for the query: '{query}'.\n"
        "Make sure the 'Posts' content tab is selected (NOT People, Jobs, "
        "Companies, Groups, Events, or Schools). If it isn't, click 'Posts'.\n"
        "Make sure results are sorted by 'Latest' (NOT 'Top match'). If a "
        "'Sort by' filter is visible, set it to 'Latest'. If a 'Date posted' "
        "filter is visible, leave it on 'Past 24 hours' or 'Past week'.\n\n"
        "HARD RULES:\n"
        "1. NEVER attempt to log in or sign up. The session is already "
        "authenticated via the attached profile. If you see a login wall, do "
        "NOT fill any form — instead reload the page or back out.\n"
        "2. NEVER click 'Sign in with Google/Apple', 'Join now', or any auth button.\n"
        "3. If a 'Sign in to see more' / cookie banner / 'Get the app' modal "
        "appears, dismiss it (X, Escape, 'Not now', or click outside).\n"
        "4. NEVER send a connection request, follow a person/company, like, "
        "comment, repost, or message anyone. Read-only browsing only.\n"
        "5. Stay on the search results feed — do NOT click into individual "
        "posts, author profiles, hashtags, or 'Show more results'. If a post's "
        "text is truncated with '…see more', click ONLY that inline 'see more' "
        "link to expand the post in place.\n\n"
        f"MAIN LOOP — repeat up to {scrolls} times, OR until you have observed "
        f"at least {top_n} distinct posts:\n"
        " 1. Dismiss any popup/modal that appeared.\n"
        " 2. For each visible post card, note: full post text (after expanding "
        "'see more' if present), author display name, author headline (the "
        "subtitle under the name, e.g. 'Founder at X'), relative timestamp "
        "(e.g. '2h', '1d'), and the post permalink (the href on the timestamp "
        "link — usually of the form /posts/<slug> or /feed/update/urn:li:activity:<id>).\n"
        " 3. Press the End key, or scroll down by ~1200px, to load more posts. "
        "Wait ~2 seconds for new cards to render (LinkedIn lazy-loads).\n\n"
        f"WHEN DONE — return STRICTLY a JSON array of the {top_n} MOST RECENT "
        "posts (sorted newest-first), each object with these keys:\n"
        '  {"text": str, "author": str, "headline": str, '
        '"posted": str, "url": str, "summary": str}\n'
        "`url` should be the absolute permalink (https://www.linkedin.com/...). "
        "`summary` should be a one-sentence gist of the post. Do not include "
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
        "platform": "linkedin",
        "query": query,
        "items": _parse_json_array(raw),
        "raw": raw,
        "success": success,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query", type=str, required=False, default=None,
        help="LinkedIn content search query (required unless --login-only).",
    )
    parser.add_argument(
        "--scrolls", type=int, default=10,
        help="Max number of scroll steps on the results feed (default: 10).",
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
            start_url="https://www.linkedin.com/login",
            profile_name=args.profile,
            no_open=args.no_open,
            instructions=">>> Log into LinkedIn in the live-preview window.",
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
        convex_platform="linkedin",
        convex_query=args.query,
    ))


if __name__ == "__main__":
    main()
