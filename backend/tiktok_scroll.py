"""Browser-Use Cloud agent that opens TikTok and scrolls the For You feed.

Spins a cloud session, prints the live-preview URL (paste into a browser to
watch in real time), then polls the task until it finishes.

Auth uses the shared 'social' Browser-Use profile (also used by
reddit_scroll.py and twitter_scroll.py). Log in once with:

    uv run python social_login.py

Or per-platform:

    uv run python tiktok_scroll.py --login-only

Normal usage (defaults to the shared 'social' profile):
    uv run python tiktok_scroll.py
    uv run python tiktok_scroll.py --scrolls 30 --query "skincare"

Docs:
    https://docs.browser-use.com/cloud/quickstart
    https://docs.browser-use.com/cloud/browser/live-preview
    https://docs.browser-use.com/cloud/browser/profiles
    https://docs.browser-use.com/cloud/guides/authentication
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

PROFILE_ENV_VAR = "BROWSER_USE_TIKTOK_PROFILE_ID"


def build_task(scrolls: int, query: str | None) -> tuple[str, str]:
    if query:
        # /search/video/?q=... goes straight to the Videos tab (skips Top/Users).
        start_url = f"https://www.tiktok.com/search/video?q={quote(query)}"
        opener = (
            "You are on the TikTok Videos search results page for the query.\n"
            "STEP 1 — Make sure the 'Videos' tab is selected (NOT Top, Users, or "
            "Hashtags). If it isn't, click the 'Videos' tab.\n"
            "STEP 2 — Click the FIRST VIDEO THUMBNAIL in the grid. A video "
            "thumbnail is a tile with a play count + duration overlay; it is NOT "
            "a creator/user card (those have a profile avatar + @handle and live "
            "under the Users tab — never click these). After clicking, the video "
            "opens in TikTok's full-screen player.\n"
        )
    else:
        start_url = "https://www.tiktok.com/foryou"
        opener = (
            "You are on the TikTok For You feed. The first video should already "
            "be playing in the main viewer.\n"
        )

    task = (
        f"{opener}\n"
        "HARD RULES — read carefully:\n"
        "1. NEVER attempt to log in, sign up, or fill any form. We are browsing as a guest.\n"
        "2. NEVER click 'Continue with Google', 'Use phone / email', or any auth button.\n"
        "3. NEVER engage with 'Verify it's you' / captcha / phone-number challenges. "
        "If one appears, close the modal (Escape or X) and continue scrolling.\n"
        "4. NEVER click on a creator profile, @handle, avatar, or user card. "
        "We only want to watch videos.\n"
        "5. If you cannot close a blocking modal, navigate back to the previous URL "
        "and keep scrolling from there.\n\n"
        "POPUP HANDLING — apply BEFORE every scroll and EVERY time the page changes:\n"
        " - Look for any modal, dialog, or overlay (login, cookies, app download, "
        "'Verify it's you', 'Log in to follow').\n"
        " - Close it: prefer the X / close button in the top-right of the modal; "
        "otherwise click 'Not now' / 'Skip' / 'Maybe later'; otherwise press Escape.\n"
        " - If clicking outside the modal dismisses it, do that. Never type anything.\n\n"
        f"MAIN LOOP — once a video is playing in the full-screen player, repeat "
        f"{scrolls} times:\n"
        " 1. Run the popup-handling step above.\n"
        " 2. Press the Down Arrow key once to advance to the next video.\n"
        "    (If Down Arrow doesn't advance, scroll the player area down by ~800px.)\n"
        " 3. Wait ~2 seconds so the video can play.\n"
        "Stay inside the video player the whole time — never navigate to a "
        "creator profile or back to the search grid.\n\n"
        f"WHEN DONE — return STRICTLY a JSON array of the {min(5, scrolls)} MOST "
        "RECENT videos you watched (newest-first), each object with these keys:\n"
        '  {"caption": str, "author": str, "handle": str, '
        '"posted": str, "url": str, "summary": str}\n'
        "`url` should be the absolute permalink (https://www.tiktok.com/@<handle>/video/<id>) "
        "if visible. `summary` is a one-sentence gist. Do not include any prose "
        "outside the JSON array."
    )
    return start_url, task


async def scrape(
    query: str | None = None,
    *,
    scrolls: int = 10,
    profile_id: str | None = None,
    profile_name: str = DEFAULT_PROFILE_NAME,
    no_profile: bool = False,
    llm: str = "browser-use-2.0",
) -> dict[str, Any]:
    """Library entrypoint: returns {'platform','query','items','raw','success'}.

    `query=None` opens the For You feed; otherwise searches.
    """
    pid = profile_id or os.environ.get(PROFILE_ENV_VAR)
    start_url, task = build_task(scrolls, query)
    success, raw = await run_scrape_collect(
        start_url=start_url,
        task=task,
        llm=llm,
        profile_name=profile_name,
        profile_id=pid,
        no_profile=no_profile,
        convex_platform="tiktok",
        convex_query=query or "",
    )
    return {
        "platform": "tiktok",
        "query": query,
        "items": parse_json_array(raw),
        "raw": raw,
        "success": success,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scrolls", type=int, default=15,
        help="How many times to scroll the feed (default: 15).",
    )
    parser.add_argument(
        "--query", type=str, default=None,
        help="If set, search TikTok for this term instead of opening For You.",
    )
    add_common_args(parser)
    args = parser.parse_args()

    if args.login_only:
        if args.no_profile:
            raise SystemExit("--login-only requires a profile; drop --no-profile.")
        asyncio.run(run_login_session(
            start_url="https://www.tiktok.com/login",
            profile_name=args.profile,
            no_open=args.no_open,
            instructions=">>> Log into TikTok in the live-preview window.",
        ))
        return

    start_url, task = build_task(args.scrolls, args.query)
    asyncio.run(run_scrape(
        start_url=start_url,
        task=task,
        llm=args.llm,
        no_open=args.no_open,
        profile_name=args.profile,
        profile_id=args.profile_id or os.environ.get(PROFILE_ENV_VAR),
        no_profile=args.no_profile,
        banner_extra={"Query:": args.query or "<For You feed>"},
        convex_platform="tiktok",
        convex_query=args.query or "",
    ))


if __name__ == "__main__":
    main()
