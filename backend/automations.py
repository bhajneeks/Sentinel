"""Reacher outreach-automation client.

Wraps the Reacher `/automations/*` endpoints used to enqueue creator
outreach. Designed to be invoked by `campaign.run_campaign_pipeline()`
*after* a marketing campaign has been generated, but the same module
can be imported and used standalone.

Two safety gates govern whether anything actually hits Reacher:

  AUTOMATIONS_ENABLED  - master switch. Default "false". If false,
                         `propose_automations_for_campaign()` returns the
                         planned payload but DOES NOT call Reacher.
  AUTOMATIONS_DRY_RUN  - secondary safety. Default "true". When true,
                         even with the master switch on, we log the
                         payload and return it without firing the POST.
                         Set to "false" to actually create the automation.

Both flags read from the environment on every call (no caching) so a
single `.env` change does not require a restart.

Suggested playbook for a campaign run (only DM is auto-built today;
the others are documented as `propose_*` helpers you can call later):

  1. DM Outreach              - default, runs from the campaign's
                                 creator shortlist
  2. Sample Request           - for creators who reply positively, queue
                                 sample shipment so they can record
  3. Target Collab Cleanup    - sweep stale invites after N days
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

import reacher

logger = logging.getLogger("uvicorn.error")

# Reacher REST paths. These are inferred from the MCP tool naming convention
# (e.g. `automations_list_automations_list_post` -> POST /automations/list).
# If Reacher's actual paths differ, override the constants below.
AUTOMATIONS_LIST_PATH = "/automations/list"
AUTOMATIONS_FILTERS_PATH = "/automations/filters"
AUTOMATION_DETAIL_PATH_TEMPLATE = "/automations/{automation_id}"
AUTOMATION_DM_CREATE_PATH = "/automations/dm"
AUTOMATION_EMAIL_CREATE_PATH = "/automations/email"
AUTOMATION_TARGET_COLLAB_CREATE_PATH = "/automations/target-collab"
AUTOMATION_TC_CLEANUP_CREATE_PATH = "/automations/tc-cleanup"
AUTOMATION_SAMPLE_REQUEST_CREATE_PATH = "/automations/sample-request"
AUTOMATION_START_PATH_TEMPLATE = "/automations/{automation_id}/start"
AUTOMATION_STOP_PATH_TEMPLATE = "/automations/{automation_id}/stop"


def is_enabled() -> bool:
    return _truthy(os.environ.get("AUTOMATIONS_ENABLED", "false"))


def is_dry_run() -> bool:
    return _truthy(os.environ.get("AUTOMATIONS_DRY_RUN", "true"))


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _request(
    method: str, path: str, *, json_body: dict | None = None,
    params: dict | None = None, shop_id: str | None = None,
) -> Any:
    headers = reacher._headers(shop_id)
    async with httpx.AsyncClient(
        base_url=reacher.REACHER_BASE_URL, timeout=30.0, headers=headers,
    ) as client:
        resp = await client.request(
            method, path, json=json_body, params=params,
        )
    if resp.status_code >= 400:
        try:
            body: Any = resp.json()
        except Exception:
            body = resp.text
        raise reacher.ReacherAPIError(resp.status_code, body)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


# ---------------------------------------------------------------------------
# Read-only ops (always safe — no gates)
# ---------------------------------------------------------------------------


async def list_automations(
    *, shop_id: str | None = None, status: str | None = None,
    automation_type: str | None = None, page: int = 1, page_size: int = 50,
) -> dict:
    body = {"page": page, "page_size": page_size}
    if status:
        body["status"] = status
    if automation_type:
        body["automation_type"] = automation_type
    return await _request(
        "POST", AUTOMATIONS_LIST_PATH, json_body=body, shop_id=shop_id,
    )


async def get_automation(automation_id: int, *, shop_id: str | None = None) -> dict:
    return await _request(
        "GET",
        AUTOMATION_DETAIL_PATH_TEMPLATE.format(automation_id=automation_id),
        shop_id=shop_id,
    )


async def get_filters(*, shop_region: str = "US", shop_id: str | None = None) -> dict:
    return await _request(
        "GET", AUTOMATIONS_FILTERS_PATH,
        params={"shop_region": shop_region}, shop_id=shop_id,
    )


# ---------------------------------------------------------------------------
# Write ops (gated by AUTOMATIONS_ENABLED + AUTOMATIONS_DRY_RUN)
# ---------------------------------------------------------------------------


async def _gated_post(path: str, payload: dict, *, shop_id: str | None,
                       label: str) -> dict:
    """Centralized gate: enabled? dry-run? otherwise POST."""
    enabled = is_enabled()
    dry_run = is_dry_run()
    plan = {
        "would_post_to": path,
        "payload": payload,
        "enabled": enabled,
        "dry_run": dry_run,
    }
    if not enabled:
        logger.info("[automations] %s: AUTOMATIONS_ENABLED=false, returning plan only", label)
        return {"status": "skipped_disabled", **plan}
    if dry_run:
        logger.info("[automations] %s: dry-run, payload logged", label)
        return {"status": "dry_run", **plan}
    logger.info("[automations] %s: POSTing to %s", label, path)
    response = await _request("POST", path, json_body=payload, shop_id=shop_id)
    return {"status": "submitted", "response": response, **plan}


async def start_automation(
    automation_id: int, *, shop_id: str | None = None,
) -> dict:
    return await _gated_post(
        AUTOMATION_START_PATH_TEMPLATE.format(automation_id=automation_id),
        {}, shop_id=shop_id, label=f"start#{automation_id}",
    )


async def stop_automation(
    automation_id: int, *, shop_id: str | None = None,
) -> dict:
    return await _gated_post(
        AUTOMATION_STOP_PATH_TEMPLATE.format(automation_id=automation_id),
        {}, shop_id=shop_id, label=f"stop#{automation_id}",
    )


# ---------------------------------------------------------------------------
# DM payload construction
# ---------------------------------------------------------------------------


# Default outreach template. {creator_name}, {hook}, {brand} substituted in.
DEFAULT_DM_TEMPLATE = (
    "Hi {creator_name} — we're {brand}, and we loved your recent content. "
    "{hook} If you'd be open to trying a sample, we'd love to send you "
    "one. No obligation, full creative freedom. Worth a chat?"
)


def _slugify(text: str, max_len: int = 50) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-")
    return (s or "campaign")[:max_len]


def _extract_creator_targets(intel: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten competitor_intel.competitors[*].creators[*] into a unique list.

    Dedupes on creatorId/handle so the same creator across two competitor
    products is not DM'd twice.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    competitors = intel.get("competitors") if isinstance(intel, dict) else None
    if not isinstance(competitors, list):
        return out
    for comp in competitors:
        creators = comp.get("creators") if isinstance(comp, dict) else None
        if not isinstance(creators, list):
            continue
        for c in creators:
            if not isinstance(c, dict):
                continue
            key = str(
                c.get("creatorId") or c.get("creator_id")
                or c.get("handle") or c.get("name") or ""
            ).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({
                "creator_id": c.get("creatorId") or c.get("creator_id"),
                "handle": c.get("handle") or c.get("username"),
                "name": c.get("name"),
                "followers": c.get("followers"),
                "gmv_28d": c.get("gmv28d") or c.get("gmv_28d"),
                "avg_views": c.get("avgViews") or c.get("avg_views"),
                "engagement": c.get("engagement"),
                "email": c.get("email"),
            })
    return out


def _first_hook(campaign_md: str) -> str:
    """Pull the first '- "..."' bullet under the `## Hooks` heading."""
    block = re.search(
        r"^##\s+Hooks\s*\n(.+?)(?:\n##\s|\Z)",
        campaign_md, flags=re.MULTILINE | re.DOTALL,
    )
    if not block:
        return ""
    for line in block.group(1).splitlines():
        m = re.search(r'[-*]\s+"?([^"]+?)"?\s*$', line.strip())
        if m and m.group(1):
            return m.group(1).strip()
    return ""


def build_dm_payload(
    *,
    intel: dict[str, Any],
    campaign_md: str,
    brand_name: str = "Aroma Cloud",
    template: str = DEFAULT_DM_TEMPLATE,
    automation_name: str | None = None,
    max_creators: int = 50,
) -> dict[str, Any]:
    """Assemble the JSON body for a Create-DM-Automation request."""
    targets = _extract_creator_targets(intel)[:max_creators]
    hook = _first_hook(campaign_md) or "We've been following your work and think you'd vibe with what we're building."

    rendered_messages = []
    for t in targets:
        rendered_messages.append({
            "creator_id": t["creator_id"],
            "handle": t["handle"],
            "message": template.format(
                creator_name=(t["name"] or t["handle"] or "there"),
                hook=hook,
                brand=brand_name,
            ),
        })

    name = automation_name or (
        f"Campaign DM "
        f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')} - {_slugify(brand_name)}"
    )
    return {
        "automation_name": name,
        "automation_type": "Message",
        "creators": [
            {
                "creator_id": t["creator_id"],
                "handle": t["handle"],
            }
            for t in targets
            if t.get("creator_id") or t.get("handle")
        ],
        "message_template": template,
        "rendered_messages": rendered_messages,
        "_meta": {
            "brand": brand_name,
            "hook_used": hook,
            "target_count": len(targets),
        },
    }


async def create_dm_automation(
    payload: dict[str, Any], *, shop_id: str | None = None,
) -> dict:
    return await _gated_post(
        AUTOMATION_DM_CREATE_PATH, payload,
        shop_id=shop_id, label="create_dm",
    )


# ---------------------------------------------------------------------------
# High-level: post-campaign hook
# ---------------------------------------------------------------------------


async def propose_automations_for_campaign(
    campaign_result: dict[str, Any], *,
    brand_name: str = "Aroma Cloud",
    template: str = DEFAULT_DM_TEMPLATE,
    max_creators: int = 50,
    shop_id: str | None = None,
) -> dict[str, Any]:
    """Build (and optionally fire) outreach automations from a campaign output.

    Currently builds:
      - DM outreach to every unique creator in `competitor_intel`

    Future:
      - sample_request / target_collab / tc_cleanup helpers (see top of file)
    """
    intel = (campaign_result.get("subagents") or {}).get("competitor_intel") or {}
    if isinstance(intel, dict) and intel.get("error"):
        return {
            "skipped": True,
            "reason": "competitor_intel subagent had an error — no targets to DM",
            "intel_error": intel.get("error"),
        }

    dm_payload = build_dm_payload(
        intel=intel,
        campaign_md=campaign_result.get("campaign_markdown", ""),
        brand_name=brand_name,
        template=template,
        max_creators=max_creators,
    )

    if not dm_payload["creators"]:
        return {
            "skipped": True,
            "reason": "no usable creator handles/ids in competitor_intel",
        }

    dm_result = await create_dm_automation(dm_payload, shop_id=shop_id)
    return {
        "dm": dm_result,
        "config": {
            "enabled": is_enabled(),
            "dry_run": is_dry_run(),
            "creators_targeted": dm_payload["_meta"]["target_count"],
            "hook_used": dm_payload["_meta"]["hook_used"],
        },
    }
