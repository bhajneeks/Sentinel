"""End-to-end probe of the scripts subagent + Notion publish.

Builds a small fake campaign payload, runs `propose_scripts_for_campaign`
with publish=True, and prints the Notion result. Safe — appends to the
target page only; never mutates anything else.

Run from backend/:
    uv run python test_scripts_notion.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv()

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import scripts


FAKE_BRIEF = "Make a marketing campaign for a hydrating tinted lip oil."
FAKE_CAMPAIGN_MD = """# Campaign: Glide Test Lip Oil

## One-line concept
A hydrating tinted lip oil that glides without tugging.

## Hooks
- "Three lip oils. One glides like silk."
- "Non-sticky lip oil that hydrates and tints in one swipe."

## Creator shortlist
- Charlett W (@charlettw)
- Angie (@angiecarranza532)
"""

FAKE_INTEL = {
    "competitors": [{
        "product": {"title": "Hydrating Tinted Lip Oil"},
        "creators": [
            {"handle": "charlettw", "name": "Charlett W", "followers": 240000},
            {"handle": "angiecarranza532", "name": "Angie", "followers": 95000},
        ],
    }],
}

FAKE_HOOKS = {
    "hooks": [
        {"title": "Lip oil glide test",
         "caption": "Drag-tested 3 lip oils — one wins on glide",
         "creator_handle": "@beautynerdkay",
         "content_tags": ["lipoil", "drag-test", "swatch"]},
        {"title": "Sticky lip gloss vs lip oil",
         "caption": "POV: you finally find a lip product that doesn't pull",
         "creator_handle": "@menter_latonia",
         "content_tags": ["lipoil", "POV", "glow"]},
    ],
}

FAKE_PULSE = {
    "topic": "tinted lip oil",
    "platforms": ["twitter"],
    "items_total": 2,
    "results": {
        "twitter": {
            "platform": "twitter",
            "success": True,
            "items": [
                {"author": "menter_latonia", "handle": "@menter_latonia",
                 "text": "An ultra-glossy tinted lip oil that nourishes lips' natural color. Non-sticky. Plush applicator.",
                 "url": "https://x.com/menter_latonia/status/123",
                 "posted": "1h"},
                {"author": "Beauty Kay", "handle": "@beautynerdkay",
                 "text": "Tested 3 lip oils today. Only one didn't tug.",
                 "url": "https://x.com/beautynerdkay/status/456",
                 "posted": "2h"},
            ],
        },
    },
}

FAKE_CONTEXT = {
    "company_docs": {
        "brand-guide.md": (
            "Brand voice: warm, curious, candid. Avoid 'elevate', 'unlock', "
            "'fuel', 'game-changing', 'liquid energy'. Don't pun on brew/espress."
        ),
    },
    "past_campaign_memory": [],
}


async def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set", file=sys.stderr)
        return 1
    if not os.environ.get("NOTION_API_KEY"):
        print("NOTION_API_KEY not set", file=sys.stderr)
        return 1
    if not os.environ.get("NOTION_SCRIPTS_PAGE_ID"):
        print("NOTION_SCRIPTS_PAGE_ID not set", file=sys.stderr)
        return 1

    print("Generating + publishing scripts...")
    result = await scripts.propose_scripts_for_campaign(
        brief=FAKE_BRIEF,
        intel=FAKE_INTEL,
        hooks=FAKE_HOOKS,
        context=FAKE_CONTEXT,
        pulse=FAKE_PULSE,
        campaign_markdown=FAKE_CAMPAIGN_MD,
        brand_name="Aroma Cloud",
        count=3,
        publish=True,
    )

    print("\n=== SCRIPTS ===")
    for i, s in enumerate(result.get("scripts", []), start=1):
        print(f"\n[{i}] {s.get('title')}")
        print(f"    creator: {s.get('creator')}  platform: {s.get('platform')}  "
              f"duration: {s.get('duration_seconds')}s")
        print(f"    hook: {s.get('hook')}")
        print(f"    sourced_from: {s.get('sourced_from')}")

    print("\n=== NOTION ===")
    print(json.dumps(result.get("notion"), indent=2, default=str))
    print(f"\npublished: {result.get('published')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
