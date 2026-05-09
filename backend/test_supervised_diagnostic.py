"""Diagnostic: spawn 4 supervised agents on a company, dump raw FOUND
output per platform, no LLM judge / no iMessage push. Lets us see which
platforms actually produce signal before the strict quality gate kicks in.

Usage:
    uv run python test_supervised_diagnostic.py "Anthropic"
    uv run python test_supervised_diagnostic.py "Anthropic" --duration 240
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv()

import supervised_agent as sa  # noqa: E402
from browser_use_common import make_client  # noqa: E402


@dataclass
class PlatformDiag:
    platform: str
    handle: sa.AgentHandle
    last_step_seen: int = 0
    raw_found: int = 0           # FOUND lines emitted by the agent
    keyword_passed: int = 0      # of those, how many pass _is_relevant
    samples: list[tuple[str, str, str, bool]] = None  # (url, author, summary, kw_pass)

    def __post_init__(self) -> None:
        if self.samples is None:
            self.samples = []


async def _poll_once(diag: PlatformDiag, company: str, client) -> None:
    try:
        task = await client.tasks.get_task(diag.handle.current_task_id)
    except Exception as exc:
        print(f"  [{diag.platform}] poll error: {exc}", flush=True)
        return

    steps = list(task.steps or [])
    new_steps = [s for s in steps if int(getattr(s, "number", 0) or 0) > diag.last_step_seen]
    if not new_steps:
        return

    for step in new_steps:
        blob_parts: list[str] = []
        for attr in ("memory", "next_goal", "evaluation_previous_goal"):
            val = getattr(step, attr, None)
            if isinstance(val, str) and val:
                blob_parts.append(val)
        blob = "\n".join(blob_parts)

        for url, author, summary in sa._extract_found_lines(blob):
            diag.raw_found += 1
            kw_pass = sa._is_relevant(f"{url} {summary}", company)
            if kw_pass:
                diag.keyword_passed += 1
            diag.samples.append((url, author, summary, kw_pass))

    diag.last_step_seen = max(int(getattr(s, "number", 0) or 0) for s in new_steps)


def _print_running_summary(diags: list[PlatformDiag], elapsed: float) -> None:
    print(f"\n=== t={elapsed:0.0f}s — per-platform raw output ===", flush=True)
    for d in diags:
        print(
            f"  [{d.platform:>8}] raw_found={d.raw_found:<3} "
            f"kw_passed={d.keyword_passed:<3} step={d.last_step_seen}",
            flush=True,
        )


def _print_final(diags: list[PlatformDiag], company: str) -> None:
    print("\n" + "=" * 72, flush=True)
    print(f"FINAL DIAGNOSTIC for company='{company}'", flush=True)
    print("=" * 72, flush=True)
    for d in diags:
        print(
            f"\n[{d.platform}]  raw_found={d.raw_found}  "
            f"keyword_passed={d.keyword_passed}",
            flush=True,
        )
        if not d.samples:
            print("  (no FOUND lines emitted — agent may be stuck or filter is too tight)", flush=True)
            continue
        for i, (url, author, summary, kw) in enumerate(d.samples[:10], 1):
            tag = "✓" if kw else "✗"
            print(f"  {i:>2}. {tag} {url}", flush=True)
            print(f"      author: {author}", flush=True)
            print(f"      summary: {summary[:140]}", flush=True)
        if len(d.samples) > 10:
            print(f"  ... +{len(d.samples) - 10} more", flush=True)


async def main(company: str, duration: int) -> None:
    if not os.environ.get("BROWSER_USE_API_KEY"):
        sys.exit("BROWSER_USE_API_KEY missing — add it to backend/.env.local")
    if not os.environ.get("CONVEX_URL"):
        print("note: CONVEX_URL is unset — agents won't appear on the dashboard.", flush=True)

    participant = "diagnostic"
    print(f"spawning 4 agents for company='{company}'...", flush=True)
    results = await sa.spawn_company_agents(
        participant=participant,
        company=company,
        run_id=None,                  # no Convex mention writes in diagnostic mode
        start_harvester=False,        # we run our own raw poller below
    )
    print(f"spawn results: {results}", flush=True)

    diags: list[PlatformDiag] = []
    for platform, handle in sa._registry.get(participant, {}).items():
        diags.append(PlatformDiag(platform=platform, handle=handle))

    if not diags:
        print("no handles spawned. nothing to poll.", flush=True)
        return

    client = make_client()
    poll_interval = 30.0
    started = time.time()
    try:
        while True:
            elapsed = time.time() - started
            if elapsed >= duration:
                break
            await asyncio.sleep(poll_interval)
            for d in diags:
                await _poll_once(d, company, client)
            _print_running_summary(diags, time.time() - started)
    finally:
        _print_final(diags, company)
        print("\nclosing all agents...", flush=True)
        try:
            n = await sa.close_all_for_participant(participant)
            print(f"closed {n} handle(s).", flush=True)
        except Exception as exc:
            print(f"close_all error: {exc}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("company", type=str)
    parser.add_argument(
        "--duration", type=int, default=240,
        help="Total seconds to run the diagnostic (default 240 = 4 min).",
    )
    args = parser.parse_args()
    asyncio.run(main(args.company, args.duration))
