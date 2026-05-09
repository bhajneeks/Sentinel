"""Shared helpers for Browser-Use Cloud scrapers (TikTok, X, Reddit, ...).

All social scrapers share a single Browser-Use profile (default name:
"social") so one login session persists cookies for every platform we visit.

This module owns:
  - profile resolution (find-by-name, create-if-missing)
  - session creation + clean shutdown
  - the create-task / poll-status loop
  - the manual-login flow (`run_login_session`)

Each platform script just defines `build_task(...)` and calls `run_scrape`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import webbrowser
from typing import Awaitable, Callable

from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv()

# Windows consoles default to cp1252 and crash on emoji in agent output.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from browser_use_sdk import AsyncBrowserUse  # noqa: E402

TERMINAL_STATUSES = {"finished", "stopped", "failed", "completed", "cancelled"}

# Single shared profile across all social platforms.
DEFAULT_PROFILE_NAME = "social"


def get_api_key() -> str:
    api_key = os.environ.get("BROWSER_USE_API_KEY")
    if not api_key:
        raise SystemExit(
            "BROWSER_USE_API_KEY is not set. Add it to backend/.env.local "
            "(get a key at https://cloud.browser-use.com/settings?tab=api-keys&new=1)"
        )
    return api_key


def make_client() -> AsyncBrowserUse:
    return AsyncBrowserUse(api_key=get_api_key())


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


async def resolve_profile_id(
    client: AsyncBrowserUse,
    *,
    profile_name: str,
    profile_id: str | None,
    no_profile: bool,
) -> str | None:
    if no_profile:
        print("Running without a profile (guest mode).")
        return None
    if profile_id:
        print(f"Using profile id from --profile-id: {profile_id}")
        return profile_id
    print(f"Resolving profile '{profile_name}'...")
    resolved = await get_or_create_profile_id(client, profile_name)
    print(f"Profile id: {resolved}")
    return resolved


async def stop_session(client: AsyncBrowserUse, session_id: str) -> None:
    """Stop the session — required to persist profile state to the profile."""
    try:
        await client.sessions.update_session(session_id, action="stop")
    except Exception as exc:
        print(f"Warning: failed to stop session {session_id}: {exc}")


def _print_session_banner(
    session_live_url: str | None,
    session_id: str,
    start_url: str,
    extra: dict[str, str] | None = None,
) -> None:
    print()
    print("=" * 72)
    print("LIVE PREVIEW (open in your browser to watch the agent):")
    print(session_live_url)
    print("=" * 72)
    print()
    print(f"Session id: {session_id}")
    print(f"Start URL:  {start_url}")
    for k, v in (extra or {}).items():
        print(f"{k:<11} {v}")


async def _run_task(
    *,
    start_url: str,
    task: str,
    llm: str,
    no_open: bool,
    profile_name: str,
    profile_id: str | None,
    no_profile: bool,
    banner_extra: dict[str, str] | None,
    silent: bool,
) -> tuple[bool, str | None]:
    """Shared body for run_scrape (CLI) and run_scrape_collect (library).

    Returns (is_success, output_text).
    """
    def log(msg: str = "") -> None:
        if not silent:
            print(msg)

    client = make_client()
    if silent:
        # resolve_profile_id prints — duplicate the inlined logic here quietly.
        if no_profile:
            resolved_profile_id: str | None = None
        elif profile_id:
            resolved_profile_id = profile_id
        else:
            resolved_profile_id = await get_or_create_profile_id(client, profile_name)
    else:
        resolved_profile_id = await resolve_profile_id(
            client,
            profile_name=profile_name,
            profile_id=profile_id,
            no_profile=no_profile,
        )

    log("Creating Browser-Use cloud session...")
    session_kwargs: dict = {"start_url": start_url}
    if resolved_profile_id:
        session_kwargs["profile_id"] = resolved_profile_id
    session = await client.sessions.create_session(**session_kwargs)

    if not silent:
        _print_session_banner(session.live_url, session.id, start_url, banner_extra)

    if not silent and not no_open and session.live_url:
        log("Opening live preview in your default browser...")
        webbrowser.open(session.live_url)

    try:
        log("Starting task...")
        task_resp = await client.tasks.create_task(
            task=task,
            session_id=session.id,
            llm=llm,
            start_url=start_url,
        )
        task_id = task_resp.id
        log(f"Task id:    {task_id}")
        log()
        log("Polling task status (Ctrl-C to stop)...")

        last_status: str | None = None
        while True:
            status = await client.tasks.get_task_status(task_id)
            if status.status != last_status:
                log(f"  status: {status.status}")
                last_status = status.status
            if status.status and status.status.lower() in TERMINAL_STATUSES:
                log()
                log("Done.")
                log(f"Success: {status.is_success}")
                if status.output:
                    log(f"Output: {status.output}")
                return bool(status.is_success), status.output
            await asyncio.sleep(3)
    finally:
        await stop_session(client, session.id)


async def run_scrape(
    *,
    start_url: str,
    task: str,
    llm: str,
    no_open: bool,
    profile_name: str,
    profile_id: str | None,
    no_profile: bool,
    banner_extra: dict[str, str] | None = None,
) -> None:
    """CLI entrypoint: prints progress, no return value."""
    await _run_task(
        start_url=start_url,
        task=task,
        llm=llm,
        no_open=no_open,
        profile_name=profile_name,
        profile_id=profile_id,
        no_profile=no_profile,
        banner_extra=banner_extra,
        silent=False,
    )


async def run_scrape_collect(
    *,
    start_url: str,
    task: str,
    llm: str = "browser-use-2.0",
    profile_name: str = DEFAULT_PROFILE_NAME,
    profile_id: str | None = None,
    no_profile: bool = False,
) -> tuple[bool, str | None]:
    """Library entrypoint: silent, returns (is_success, raw_agent_output)."""
    return await _run_task(
        start_url=start_url,
        task=task,
        llm=llm,
        no_open=True,
        profile_name=profile_name,
        profile_id=profile_id,
        no_profile=no_profile,
        banner_extra=None,
        silent=True,
    )


async def run_login_session(
    *,
    start_url: str,
    profile_name: str,
    no_open: bool,
    instructions: str,
) -> None:
    """Open a session pointing at a login page, wait for the user to finish, then stop.

    Stopping the session is what flushes cookies into the profile, so cookies
    only persist if the user lets this function complete normally.
    """
    client = make_client()
    profile_id = await get_or_create_profile_id(client, profile_name)
    print(f"Using profile '{profile_name}' (id={profile_id}).")

    print("Creating Browser-Use cloud session...")
    session = await client.sessions.create_session(
        start_url=start_url, profile_id=profile_id
    )
    _print_session_banner(session.live_url, session.id, start_url)

    if not no_open and session.live_url:
        print("Opening live preview in your default browser...")
        webbrowser.open(session.live_url)

    try:
        print()
        print(instructions)
        print(">>> When you're done, come back here and press Enter to save & exit.")
        await asyncio.get_event_loop().run_in_executor(None, input)
        print("Stopping session to persist cookies into the profile...")
    finally:
        await stop_session(client, session.id)


def add_common_args(parser, *, default_profile: str = DEFAULT_PROFILE_NAME) -> None:
    """Attach the standard --llm / --profile / --no-open / etc flags."""
    parser.add_argument(
        "--llm", type=str, default="browser-use-2.0",
        help="Browser-Use LLM id (default: browser-use-2.0).",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="Don't auto-open the live preview in the default browser.",
    )
    parser.add_argument(
        "--profile", type=str, default=default_profile,
        help=f"Browser-Use profile NAME to attach (default: {default_profile}).",
    )
    parser.add_argument(
        "--profile-id", type=str, default=None,
        help="Browser-Use profile ID. Takes precedence over --profile.",
    )
    parser.add_argument(
        "--no-profile", action="store_true",
        help="Don't attach any profile (guest browsing).",
    )
    parser.add_argument(
        "--login-only", action="store_true",
        help="Open a session for manual login, then stop to persist cookies.",
    )


__all__ = [
    "DEFAULT_PROFILE_NAME",
    "TERMINAL_STATUSES",
    "add_common_args",
    "get_api_key",
    "get_or_create_profile_id",
    "make_client",
    "resolve_profile_id",
    "run_login_session",
    "run_scrape",
    "run_scrape_collect",
    "stop_session",
]
