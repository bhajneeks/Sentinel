"""One-shot multi-platform login for the shared 'social' Browser-Use profile.

Opens a single cloud session attached to the shared profile, then waits while
you sign into TikTok, X, and Reddit in the live-preview window. When you
press Enter, the session is stopped and cookies for ALL three platforms are
persisted into the same profile.

Subsequent runs of tiktok_scroll.py / twitter_scroll.py / reddit_scroll.py
default to this same profile, so they all browse signed-in.

Usage:
    uv run python social_login.py
    uv run python social_login.py --profile my-social
    uv run python social_login.py --start tiktok    # land on TikTok login first
"""

from __future__ import annotations

import argparse
import asyncio

from browser_use_common import DEFAULT_PROFILE_NAME, run_login_session

LOGIN_URLS = {
    "tiktok": "https://www.tiktok.com/login",
    "twitter": "https://x.com/i/flow/login",
    "x": "https://x.com/i/flow/login",
    "reddit": "https://www.reddit.com/login",
    "hub": "https://www.google.com",  # neutral landing page
}

INSTRUCTIONS = (
    ">>> One session, three logins. In the live-preview window, sign in to:\n"
    ">>>   1. TikTok    — https://www.tiktok.com/login\n"
    ">>>   2. X         — https://x.com/i/flow/login\n"
    ">>>   3. Reddit    — https://www.reddit.com/login\n"
    ">>> You can switch tabs / paste URLs in the address bar inside the preview.\n"
    ">>> When ALL THREE are logged in, come back here and press Enter — that\n"
    ">>> stops the session and saves every site's cookies into the shared profile."
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", type=str, default=DEFAULT_PROFILE_NAME,
        help=f"Shared profile name to log into (default: {DEFAULT_PROFILE_NAME}).",
    )
    parser.add_argument(
        "--start", type=str, default="hub",
        choices=sorted(LOGIN_URLS.keys()),
        help="Which page to open first (default: hub — a neutral start page).",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="Don't auto-open the live preview in the default browser.",
    )
    args = parser.parse_args()

    asyncio.run(run_login_session(
        start_url=LOGIN_URLS[args.start],
        profile_name=args.profile,
        no_open=args.no_open,
        instructions=INSTRUCTIONS,
    ))


if __name__ == "__main__":
    main()
