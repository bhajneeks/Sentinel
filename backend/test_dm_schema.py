"""End-to-end test of the DM-automation create flow.

Exercises three things:
  1. The shape produced by `automations.build_dm_payload` matches the docs.
  2. The `_gated_post` headers (Idempotency-Key, X-Created-Via, X-Dry-Run)
     are forwarded correctly.
  3. Reacher's validator accepts the body (or tells us what to fix).

Runs with `AUTOMATIONS_ENABLED=true` + `reacher_dry_run=True` so we send
the real HTTP request but Reacher does not persist. Safe to iterate.
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

import automations
import reacher

SHOP_ID = "1183"  # Grocery Stars (US, pro_new)


# Minimal fake intel payload that mimics what the campaign pipeline produces.
FAKE_INTEL = {
    "competitor_count": 1,
    "competitors": [{
        "product": {"product_id": "demo-1"},
        "creators": [
            {"creatorId": "7495440326467815830", "handle": "charlettw",
             "name": "Charlett W"},
            {"creatorId": "7494505524907313891", "handle": "angiecarranza532",
             "name": "Angie"},
        ],
        "videos": [],
    }],
}

FAKE_CAMPAIGN_MD = """# Campaign: Glossy Glow Tinted Lip Oil

## Hooks
- "An ultra-glossy tinted lip oil that nourishes your lips' natural color."
- "Achieve glamorous makeup looks with our high-gloss tinted lip oil."

## Creator shortlist
- Demo creators above.
"""


async def main() -> int:
    if not os.environ.get("REACHER_API_KEY"):
        print("REACHER_API_KEY not set — abort.", file=sys.stderr)
        return 1

    payload = automations.build_dm_payload(
        intel=FAKE_INTEL,
        campaign_md=FAKE_CAMPAIGN_MD,
        brand_name="Aroma Cloud",
    )
    print("=" * 72)
    print("BUILT PAYLOAD (will go to POST /automations/dm):")
    print(json.dumps(payload, indent=2, default=str))

    # Force the gate open for this probe (env stays untouched outside this proc).
    os.environ["AUTOMATIONS_ENABLED"] = "true"
    os.environ["AUTOMATIONS_DRY_RUN"] = "false"

    print()
    print("=" * 72)
    print("FIRING create_dm_automation with reacher_dry_run=True ...")
    try:
        result = await automations.create_dm_automation(
            payload, shop_id=SHOP_ID, reacher_dry_run=True,
        )
    except reacher.ReacherAPIError as exc:
        print(f"\n!! ReacherAPIError {exc.status}")
        print(json.dumps(exc.body, indent=2) if isinstance(exc.body, (dict, list)) else exc.body)
        return 2
    except Exception as exc:
        print(f"\n!! Exception: {type(exc).__name__}: {exc}")
        return 3

    print()
    print("RESULT:")
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
