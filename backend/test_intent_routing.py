"""Test Rachel's intent routing without going through iMessage.

Calls `agent.generate_reply()` directly and stubs the expensive downstream
calls (Convex, Browser-Use cloud sessions, full campaign pipeline) so we
can verify routing fast on Windows or any non-macOS machine.

Two tests:
  1. TRACKING — message has a URL + 'track' verb -> expects track_company
  2. MARKETING — message says 'make a marketing campaign' -> expects
                 create_marketing_campaign
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv()

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import agent
import campaign
import convex_client as cx
import scraper_stream
import supervised_agent
import tools


ROOT = Path(__file__).resolve().parent
TRACKED_DIR = (ROOT / ".." / "data" / "tracked").resolve()


# Tools fired during the most recent generate_reply call. Reset per test.
_tool_calls_seen: list[tuple[str, str]] = []
_orig_dispatch = tools.dispatch


async def _logging_dispatch(name: str, raw_args: str, *, participant: str) -> str:
    _tool_calls_seen.append((name, raw_args))
    print(f"  [tool] {name}({raw_args[:120]})")
    return await _orig_dispatch(name, raw_args, participant=participant)


# --- Stubs for the expensive downstream paths ---------------------------------


async def _stub_create_run(participant, company, link):
    return f"fake-run-{participant[:8]}"


async def _stub_active_browser_count():
    return 0


async def _stub_start_session(run_id, platform, query, *, browser_backed=False):
    return f"fake-session-{platform}"


async def _stub_spawn_company_agents(participant, company, *, run_id=None, overrides=None):
    # Mimics the real return shape so Rachel can summarize it.
    return {"linkedin": "started", "x": "started", "reddit": "started", "tiktok": "started"}


async def _stub_run_scraper(*, run_id, session_id, platform, query):
    # Fire-and-forget; would normally take 30-90s.
    return None


_FAKE_CAMPAIGN = {
    "brief": "stub",
    "extracted_query": "tinted lip oil",
    "campaign_markdown": (
        "# Campaign: Stubbed Glide Test\n\n"
        "## One-line concept\nA glossy lip oil that glides without tugging.\n\n"
        "## Hooks\n- 'Three lip oils. One glides like silk.'\n\n"
        "## Memory note\nstubbed test memory.\n"
    ),
    "memory_note": "stubbed test memory.",
    "saved_to": "C:/fake/path.md",
    "subagents": {
        "competitor_intel": {"competitor_count": 0, "competitors": []},
        "trending_hooks": {"hooks": []},
        "company_context": {"loaded_files": ["brand-guide.md"], "past_campaign_count": 0},
    },
    "scripts": {
        "scripts": [{"title": "Demo Script", "creator": "TBD"}],
        "notion": {"skipped": True, "reason": "publish_disabled"},
        "published": False,
    },
    "automations": {
        "dm": {"status": "skipped_disabled", "would_post_to": "/automations/dm"},
        "config": {"enabled": False, "dry_run": True, "creators_targeted": 0},
    },
}


async def _stub_run_campaign_pipeline(brief, **kwargs):
    return {**_FAKE_CAMPAIGN, "brief": brief}


# --- Test cases ---------------------------------------------------------------


def _stub_context():
    return [
        patch.object(cx, "create_run", side_effect=_stub_create_run),
        patch.object(cx, "active_browser_count", side_effect=_stub_active_browser_count),
        patch.object(cx, "start_session", side_effect=_stub_start_session),
        patch.object(supervised_agent, "spawn_company_agents",
                     side_effect=_stub_spawn_company_agents),
        patch.object(scraper_stream, "run_scraper", side_effect=_stub_run_scraper),
        patch.object(campaign, "run_campaign_pipeline",
                     side_effect=_stub_run_campaign_pipeline),
        patch.object(tools, "dispatch", side_effect=_logging_dispatch),
    ]


async def test_tracking() -> bool:
    print("=" * 72)
    print("TEST 1 — TRACKING pipeline")
    print("=" * 72)
    _tool_calls_seen.clear()

    files_before = set(TRACKED_DIR.glob("*.md")) if TRACKED_DIR.exists() else set()
    user_msg = "yo can u track openai for me, https://openai.com"

    patches = _stub_context()
    for p in patches:
        p.start()
    try:
        reply = await agent.generate_reply(
            [("inbound", user_msg)], participant="test-tracking",
        )
    finally:
        for p in reversed(patches):
            p.stop()

    print(f"\n  USER:   {user_msg}")
    print(f"  RACHEL: {reply}")
    print(f"\n  Tools fired: {[name for name, _ in _tool_calls_seen]}")

    files_after = set(TRACKED_DIR.glob("*.md")) if TRACKED_DIR.exists() else set()
    new_files = files_after - files_before
    print(f"  New data/tracked/ files: {[f.name for f in new_files] or 'none'}")
    if new_files:
        print(f"  Sample contents:\n{'-' * 50}")
        print(next(iter(new_files)).read_text(encoding="utf-8"))
        print("-" * 50)

    fired = {name for name, _ in _tool_calls_seen}
    ok_route = "track_company" in fired
    ok_persist = len(new_files) >= 1
    ok_no_marketing = "create_marketing_campaign" not in fired
    print(f"\n  routed_to_track_company: {ok_route}")
    print(f"  persisted_to_data_tracked: {ok_persist}")
    print(f"  did_not_route_to_marketing: {ok_no_marketing}")
    passed = ok_route and ok_persist and ok_no_marketing
    print(f"  RESULT: {'PASS' if passed else 'FAIL'}\n")
    return passed


async def test_marketing() -> bool:
    print("=" * 72)
    print("TEST 2 — MARKETING CAMPAIGN pipeline")
    print("=" * 72)
    _tool_calls_seen.clear()

    user_msg = "make me a marketing campaign for hydrating tinted lip oil"
    patches = _stub_context()
    for p in patches:
        p.start()
    try:
        reply = await agent.generate_reply(
            [("inbound", user_msg)], participant="test-marketing",
        )
    finally:
        for p in reversed(patches):
            p.stop()

    print(f"\n  USER:   {user_msg}")
    print(f"  RACHEL: {reply}")
    print(f"\n  Tools fired: {[name for name, _ in _tool_calls_seen]}")

    fired = {name for name, _ in _tool_calls_seen}
    ok_route = "create_marketing_campaign" in fired
    ok_no_tracking = "track_company" not in fired
    print(f"\n  routed_to_create_marketing_campaign: {ok_route}")
    print(f"  did_not_route_to_tracking: {ok_no_tracking}")
    passed = ok_route and ok_no_tracking
    print(f"  RESULT: {'PASS' if passed else 'FAIL'}\n")
    return passed


async def test_list_topics() -> bool:
    print("=" * 72)
    print("TEST 3 — list_tracked_topics tool")
    print("=" * 72)
    _tool_calls_seen.clear()

    user_msg = "what other topics are u tracking rn?"
    patches = _stub_context()
    for p in patches:
        p.start()
    try:
        reply = await agent.generate_reply(
            [("inbound", user_msg)], participant="test-list",
        )
    finally:
        for p in reversed(patches):
            p.stop()

    print(f"\n  USER:   {user_msg}")
    print(f"  RACHEL: {reply}")
    print(f"\n  Tools fired: {[name for name, _ in _tool_calls_seen]}")

    fired = {name for name, _ in _tool_calls_seen}
    ok_route = "list_tracked_topics" in fired
    print(f"\n  routed_to_list_tracked_topics: {ok_route}")
    passed = ok_route
    print(f"  RESULT: {'PASS' if passed else 'FAIL'}\n")
    return passed


async def test_upsert_no_duplicate_files() -> bool:
    print("=" * 72)
    print("TEST 4 — track same company twice -> single file (upsert)")
    print("=" * 72)
    _tool_calls_seen.clear()

    company_slug = "lotus-ai"
    expected_path = TRACKED_DIR / f"{company_slug}.md"
    # Clean prior runs of this test for a deterministic fixture.
    if expected_path.exists():
        expected_path.unlink()

    patches = _stub_context()
    for p in patches:
        p.start()
    try:
        # Round 1
        await agent.generate_reply(
            [("inbound", "track lotus ai for me at https://lotus.ai")],
            participant="test-upsert",
        )
        round1_runs = (
            tools._summarize_tracked_record(expected_path)["runs"]
            if expected_path.exists() else 0
        )
        # Round 2: re-track the SAME company (different URL too).
        await agent.generate_reply(
            [
                ("inbound", "track lotus ai for me at https://lotus.ai"),
                ("outbound", "got it, on it"),
                ("inbound", "actually also pull from https://lotus.ai/blog"),
            ],
            participant="test-upsert",
        )
    finally:
        for p in reversed(patches):
            p.stop()

    matching_files = list(TRACKED_DIR.glob(f"*{company_slug}*.md"))
    summary = (
        tools._summarize_tracked_record(expected_path)
        if expected_path.exists() else {}
    )
    print(f"\n  files matching '*{company_slug}*': {[f.name for f in matching_files]}")
    print(f"  round 1 runs count: {round1_runs}")
    print(f"  final runs count:   {summary.get('runs')}")
    print(f"  user_comment_count: {summary.get('user_comment_count')}")
    if expected_path.exists():
        print(f"  file contents:\n{'-' * 50}")
        print(expected_path.read_text(encoding="utf-8"))
        print("-" * 50)

    ok_single = len(matching_files) == 1
    ok_runs_grew = summary.get("runs", 0) >= max(2, round1_runs + 1)
    print(f"\n  single_file_only: {ok_single}")
    print(f"  runs_count_grew: {ok_runs_grew}")
    passed = ok_single and ok_runs_grew
    print(f"  RESULT: {'PASS' if passed else 'FAIL'}\n")
    return passed


async def test_user_comment_capture() -> bool:
    print("=" * 72)
    print("TEST 5 — user comment captured into tracking file")
    print("=" * 72)
    _tool_calls_seen.clear()

    company_slug = "anthropic"
    expected_path = TRACKED_DIR / f"{company_slug}.md"
    if expected_path.exists():
        expected_path.unlink()

    # Pre-populate the active company so note_user_comment has a default.
    tools._active_company_by_participant["test-comment"] = "anthropic"

    patches = _stub_context()
    for p in patches:
        p.start()
    try:
        # First track the company.
        await agent.generate_reply(
            [("inbound", "track anthropic at https://anthropic.com")],
            participant="test-comment",
        )
        # Then react with an opinion.
        await agent.generate_reply(
            [
                ("inbound", "track anthropic at https://anthropic.com"),
                ("outbound", "got it, watching them"),
                ("inbound", "ngl their pricing on the new opus tier feels kinda steep"),
            ],
            participant="test-comment",
        )
    finally:
        for p in reversed(patches):
            p.stop()

    summary = (
        tools._summarize_tracked_record(expected_path)
        if expected_path.exists() else {}
    )
    print(f"\n  user_comment_count: {summary.get('user_comment_count')}")
    fired = {name for name, _ in _tool_calls_seen}
    print(f"  Tools fired across both turns: {sorted(fired)}")
    if expected_path.exists():
        body = expected_path.read_text(encoding="utf-8")
        print(f"  file contents (tail):\n{'-' * 50}")
        print(body)
        print("-" * 50)

    ok_called = "note_user_comment" in fired
    ok_persisted = summary.get("user_comment_count", 0) >= 1
    print(f"\n  rachel_called_note_user_comment: {ok_called}")
    print(f"  comment_in_file: {ok_persisted}")
    passed = ok_called and ok_persisted
    print(f"  RESULT: {'PASS' if passed else 'FAIL'}\n")
    return passed


async def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set", file=sys.stderr)
        return 1

    results = [
        ("tracking", await test_tracking()),
        ("marketing", await test_marketing()),
        ("list_topics", await test_list_topics()),
        ("upsert", await test_upsert_no_duplicate_files()),
        ("user_comment", await test_user_comment_capture()),
    ]
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for name, ok in results:
        print(f"  {name:<14} {'PASS' if ok else 'FAIL'}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
