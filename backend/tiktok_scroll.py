"""Browser-Use Cloud agent that opens TikTok and scrolls the For You feed.

Spins a cloud session, prints the live-preview URL (paste into a browser to
watch in real time), then polls the task until it finishes.

Persistent login via Browser-Use Profiles
-----------------------------------------
TikTok aggressively prompts for login; without a profile the agent has to
re-authenticate every run. We attach a named Profile (default: "tiktok") to
the session so cookies / localStorage persist.

First-time login (one-off, ~1 min):
    uv run python tiktok_scroll.py --login-only

That opens a live preview, pauses, lets you log into TikTok manually in the
embedded browser, then stops the session — which persists cookies into the
profile. Every subsequent run reuses it automatically.

Normal usage:
    # set in .env.local: BROWSER_USE_API_KEY=bu_...
    uv run python tiktok_scroll.py
    uv run python tiktok_scroll.py --scrolls 30 --query "skincare"
    uv run python tiktok_scroll.py --profile alt-account

Docs:
    https://docs.browser-use.com/cloud/quickstart
    https://docs.browser-use.com/cloud/browser/live-preview
    https://docs.browser-use.com/cloud/browser/profiles
"""

from __future__ import annotations

import argparse
import asyncio
import os
import webbrowser
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv()

from browser_use_sdk import AsyncBrowserUse  # noqa: E402

TERMINAL_STATUSES = {"finished", "stopped", "failed", "completed", "cancelled"}


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
        "When done, return the captions and creator handles of the 5 most recent "
        "videos you saw."
    )
    return start_url, task


async def get_or_create_profile_id(client: AsyncBrowserUse, name: str) -> str:
    """Find a profile by name (paginating client-side); create it if missing."""
    page = 1
    while True:
        resp = await client.profiles.list_profiles(page_size=100, page_number=page)
        for p in resp.items or []:
            if p.name == name:
                return p.id
        if not resp.items or len(resp.items) < 100:
            break
        page += 1
    profile = await client.profiles.create_profile(name=name)
    print(f"Created new profile '{name}' (id={profile.id}).")
    return profile.id


async def stop_session(client: AsyncBrowserUse, session_id: str) -> None:
    """Stop the session — required to persist profile state to the profile."""
    try:
        await client.sessions.update_session(session_id, action="stop")
    except Exception as exc:
        print(f"Warning: failed to stop session {session_id}: {exc}")


async def run(
    scrolls: int,
    query: str | None,
    llm: str,
    no_open: bool,
    profile_name: str,
    login_only: bool,
) -> None:
    api_key = os.environ.get("BROWSER_USE_API_KEY")
    if not api_key:
        raise SystemExit(
            "BROWSER_USE_API_KEY is not set. Add it to backend/.env.local "
            "(get a key at https://cloud.browser-use.com/settings?tab=api-keys&new=1)"
        )

    client = AsyncBrowserUse(api_key=api_key)

    print(f"Resolving profile '{profile_name}'...")
    profile_id = await get_or_create_profile_id(client, profile_name)
    print(f"Profile id: {profile_id}")

    start_url, task = build_task(scrolls, query)
    if login_only:
        # Drop the agent into TikTok's homepage so the user can log in manually.
        start_url = "https://www.tiktok.com/login"

    print("Creating Browser-Use cloud session...")
    session = await client.sessions.create_session(
        profile_id=profile_id,
        start_url=start_url,
    )

    print()
    print("=" * 72)
    print("LIVE PREVIEW (open in your browser to watch the agent):")
    print(session.live_url)
    print("=" * 72)
    print()
    print(f"Session id: {session.id}")
    print(f"Start URL:  {start_url}")

    if not no_open and session.live_url:
        print("Opening live preview in your default browser...")
        webbrowser.open(session.live_url)

    try:
        if login_only:
            print()
            print(">>> Log into TikTok in the live-preview window.")
            print(">>> When you're done, come back here and press Enter to save & exit.")
            await asyncio.get_event_loop().run_in_executor(None, input)
            print("Stopping session to persist cookies into the profile...")
            return

        print("Starting task...")
        task_resp = await client.tasks.create_task(
            task=task,
            session_id=session.id,
            llm=llm,
            start_url=start_url,
        )
        task_id = task_resp.id
        print(f"Task id:    {task_id}")
        print()
        print("Polling task status (Ctrl-C to stop)...")

        last_status: str | None = None
        while True:
            status = await client.tasks.get_task_status(task_id)
            if status.status != last_status:
                print(f"  status: {status.status}")
                last_status = status.status
            if status.status and status.status.lower() in TERMINAL_STATUSES:
                print()
                print("Done.")
                print(f"Success: {status.is_success}")
                if status.output:
                    print("Output:", status.output)
                break
            await asyncio.sleep(3)
    finally:
        await stop_session(client, session.id)


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
    parser.add_argument(
        "--llm", type=str, default="browser-use-2.0",
        help="Browser-Use LLM id (default: browser-use-2.0).",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="Don't auto-open the live preview in the default browser.",
    )
    parser.add_argument(
        "--profile", type=str, default="tiktok",
        help="Browser-Use profile name to attach (created if missing).",
    )
    parser.add_argument(
        "--login-only", action="store_true",
        help="Open a session for manual login, then stop to persist cookies.",
    )
    args = parser.parse_args()
    asyncio.run(run(
        args.scrolls, args.query, args.llm, args.no_open,
        args.profile, args.login_only,
    ))


if __name__ == "__main__":
    main()
