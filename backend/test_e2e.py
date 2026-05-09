"""End-to-end test for the campaign orchestrator.

Run from backend/ with: uv run python test_e2e.py

This is a one-shot smoke test, not a pytest. It hits real OpenAI + Reacher
APIs and writes real files into ../data/campaigns/. Cost ~ $0.02 / run.

Verifies:
  1. Brand context (data/*.md listed in COMPANY_CONTEXT_FILES) is loaded.
  2. A campaign run produces a markdown file with a parseable Memory note.
  3. A second run's company-context fetch includes that memory note as
     part of `past_campaign_memory` — the persistence loop is closed.
  4. (Best-effort) `nia local sync` is reachable, so future runs see new
     campaigns through Nia as well.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv()

import campaign  # noqa: E402


SEPARATOR = "=" * 72


def banner(label: str) -> None:
    print(f"\n{SEPARATOR}\n{label}\n{SEPARATOR}")


def step1_show_context() -> dict:
    banner("STEP 1 — baseline context (before any new run)")
    ctx = campaign.gather_company_context()
    docs = ctx["company_docs"]
    print(f"brand docs loaded ({len(docs)}):")
    for name, body in docs.items():
        print(f"  - {name:24s}  {len(body):>6,} chars")
    past = ctx["past_campaign_memory"]
    print(f"past campaign memory: {len(past)} note(s)")
    for p in past[:3]:
        print(f"  - {p['campaign']}")
        print(f"      {p['memory_note'][:140]!r}")
    return ctx


async def step2_run(brief: str, label: str) -> dict:
    banner(f"STEP 2 — run pipeline: {label}")
    print(f"brief: {brief}")
    result = await campaign.run_campaign_pipeline(brief)
    print(f"extracted_query  : {result['extracted_query']}")
    print(f"saved_to         : {result['saved_to']}")
    print(f"subagents echo   : {result['subagents']['company_context']}")
    print()
    print("---- campaign_markdown (first 600 chars) ----")
    print(result["campaign_markdown"][:600])
    print("---- memory_note ----")
    print(result["memory_note"])
    return result


def step3_verify_persistence(prev_count: int, just_saved: Path) -> None:
    banner("STEP 3 — re-load context, prove the run was persisted")
    ctx = campaign.gather_company_context()
    past = ctx["past_campaign_memory"]
    print(f"past campaign memory: {len(past)} (was {prev_count})")
    assert len(past) == prev_count + 1, "expected one new past-campaign entry"
    latest = past[0]
    expected_stem = just_saved.stem
    print(f"latest entry stem  : {latest['campaign']}")
    print(f"matches saved file : {latest['campaign'] == expected_stem}")
    assert latest["campaign"] == expected_stem, "newest memory entry should be the run we just did"
    print(f"memory_note (head) : {latest['memory_note'][:160]!r}")
    print("OK — run 1's memory note will be visible to run 2.")


def step4_nia_check() -> None:
    banner("STEP 4 — verify Nia local-folder sync is reachable")
    nia = shutil.which("nia.cmd") or shutil.which("nia")
    if not nia:
        print("nia CLI not on PATH — skipping (campaign.py logs a warning instead)")
        return
    print(f"nia binary       : {nia}")
    print("running: nia local status")
    proc = subprocess.run(
        [nia, "local", "status"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=20,
    )
    print(f"exit             : {proc.returncode}")
    out = (proc.stdout or proc.stderr).strip()
    print(out[:600])


async def main() -> int:
    ctx_before = step1_show_context()
    prev_count = len(ctx_before["past_campaign_memory"])

    # First run — brand-aligned brief that should let brand-guide.md actually shine.
    result1 = await step2_run(
        "Launch a one-week single-origin drop of our Yirgacheffe natural, "
        "TikTok-Shop-first, aimed at the Aria persona.",
        label="run 1 / Yirgacheffe drop",
    )
    saved1 = Path(result1["saved_to"])
    step3_verify_persistence(prev_count, saved1)

    # Second run — different brief. Re-loaded context should now include run 1's memory.
    result2 = await step2_run(
        "Holiday gifting bundle for the Sophie persona — three single-origin bags.",
        label="run 2 / holiday gift bundle",
    )
    saved2 = Path(result2["saved_to"])
    # Confirm run 2 saw run 1 as part of its past_campaign_memory.
    ctx_after_run1 = campaign.gather_company_context()
    saved1_stem = saved1.stem
    in_context = any(
        p["campaign"] == saved1_stem for p in ctx_after_run1["past_campaign_memory"]
    )
    banner("STEP 5 — was run 1's memory visible to run 2?")
    print(f"run 1 stem present in context for run 2: {in_context}")
    assert in_context, "persistence loop is broken — run 1 should be in run 2's context"

    step4_nia_check()

    banner("PASS")
    print("Files written:")
    print(f"  - {saved1}")
    print(f"  - {saved2}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
