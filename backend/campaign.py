"""Marketing campaign orchestrator.

Takes a free-text brief like "Make a marketing campaign for a lip gloss product"
and fans out three subagents in parallel:

  1. Reacher product/creator subagent   - find similar products on TikTok Shop
                                          and the creators driving their sales.
  2. Reacher trending-hooks subagent    - pull top trending videos in the same
                                          space and extract hook-relevant
                                          metadata (titles, AI tags, captions).
  3. Nozomio context subagent           - load the company brand overview from
                                          the local Nia data folder plus a
                                          compressed history of past campaigns.

The three streams feed a final LLM call that produces the campaign markdown
*plus* a short "memory note". The whole thing is written to
`data/campaigns/<timestamp>-<slug>.md` and `nia local sync` is fired in the
background so future runs see this campaign as part of context.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import agent
import automations
import reacher
import scripts
import social_pulse

logger = logging.getLogger("uvicorn.error")

ROOT = Path(__file__).resolve().parent
DATA_DIR = (ROOT / ".." / "data").resolve()
CAMPAIGNS_DIR = DATA_DIR / "campaigns"

# Persistent company context. Files are loaded in this order; missing files
# are skipped silently. Add new top-level brand docs here to expose them to
# every campaign run.
COMPANY_CONTEXT_FILES: tuple[str, ...] = (
    "company-overview.md",
    "brand-guide.md",
    "product-roadmap.md",
)

NIA = shutil.which("nia.cmd") or shutil.which("nia") or "nia"

PRODUCT_EXTRACTION_PROMPT = """You extract a TikTok Shop product search query
from a marketing brief.

Return ONLY a JSON object of the form:
  {"query": "<short search-friendly product description, <80 chars>"}

Strip brand names. Focus on the product type and one or two distinguishing
attributes (e.g. "tinted lip oil", "matcha protein powder", "silk hair scrunchie").
"""

CAMPAIGN_SYSTEM_PROMPT = """You are the lead brand marketer at Nozomio.

You will receive a JSON payload with:
  - brief                : the team's request
  - competitor_intel     : similar TikTok Shop products + the creators driving them
  - trending_hooks       : top-performing videos in the same product space
  - social_pulse         : OPTIONAL. Recent posts about the topic across X,
                           Reddit, LinkedIn, TikTok (Browser-Use scrapers).
                           When present, use it for live cultural context and
                           to ground hooks in real conversations happening now.
                           If absent or `null`, ignore.
  - company_context      : Nozomio brand overview and a compressed history of past
                           campaigns (use these for voice + to avoid repeating ideas)

Produce a marketing campaign in this EXACT structure (markdown):

# Campaign: <short distinctive name>

## One-line concept
<single sentence>

## Hooks
- 3-5 short hook lines, each <=15 words, drawn from what is trending

## Creator shortlist
- 3-7 bullets. RULES:
    * If `competitor_intel.competitors[*].creators` or `trending_hooks.hooks[*]`
      contains real handles, name them and cite the source field verbatim.
    * Otherwise, describe creator ARCHETYPES only (e.g. "specialty coffee
      educator, 50k–150k followers"). Do NOT invent specific handles or
      specific follower counts. Do NOT borrow handles from your training data.
    * People named in `company_context.company_docs` (e.g. brand-guide examples)
      are OK to reference as illustrative comparables, not as outreach targets.
    * If both Reacher streams are empty, say so explicitly in one bullet
      (e.g. "Reacher returned no matches for this query — shortlist below is
      archetype-only, validate before outreach").

## Content plan
- 3-5 video ideas (platform + format + 1-line angle each)

## Risks / brand checks
- 1-3 bullets

## Memory note
<one paragraph, <=120 words. Compresses THIS campaign's distinctive choices
so future campaigns can stay consistent and avoid duplication. Reference brand
decisions and creator angles, NOT the brief verbatim.>

---

HARD CONSTRAINTS — apply silently, do not mention them in the output:
* Obey every "No" / "Did not work" rule in `company_context.company_docs.brand-guide.md`.
  In particular: never use the words "elevate", "unlock", "fuel", "game-changing",
  "liquid energy", or any pun on "brew/brewed/espress" beyond plain product use.
* Never invent a TikTok / IG handle or a specific follower count.
* If a Reacher subagent slot is `{"error": ...}` or empty, acknowledge that
  in the Risks section in one short line.
"""


async def extract_product_query(brief: str) -> str:
    """Use the LLM to pull a clean product search query out of a free-text brief."""
    raw = await agent.chat_completion(
        messages=[
            {"role": "system", "content": PRODUCT_EXTRACTION_PROMPT},
            {"role": "user", "content": brief},
        ],
        max_tokens=120,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    try:
        parsed = json.loads(raw)
        query = (parsed.get("query") or "").strip()
    except json.JSONDecodeError:
        query = ""
    return (query or brief)[:200]


def _summarize_video_for_hook(video: dict[str, Any]) -> dict[str, Any]:
    """Pull only hook-relevant fields out of a Reacher video object."""
    keep = (
        "title", "description", "caption", "hook", "tags", "content_tags",
        "topics", "ai_tags", "creator_handle", "creator_name",
        "views", "likes", "engagement", "gmv", "url", "video_url",
    )
    return {k: video[k] for k in keep if k in video}


def _broaden(query: str) -> str | None:
    """Fallback: keep only the last 1-2 words of a multi-word query.
    Returns None if the query is already that short."""
    words = [w for w in query.replace(",", " ").split() if w]
    if len(words) <= 2:
        return None
    return " ".join(words[-2:])


def _default_creators_per_product() -> int:
    """Reacher returns up to 100 per call. Default is dialed up so the
    creator pool actually feeds the DM automation (was 6 — too small)."""
    try:
        return max(1, min(100, int(os.environ.get("CAMPAIGN_CREATORS_PER_PRODUCT", "30"))))
    except ValueError:
        return 30


async def gather_competitor_intel_for_product_id(
    product_id: str,
    *,
    creators_per_product: int | None = None,
    videos_per_product: int = 6,
    shop_id: str | None = None,
) -> dict[str, Any]:
    """Subagent 1, direct path: caller already knows the competitor product_id."""
    cpp = creators_per_product or _default_creators_per_product()
    headers = reacher._headers(shop_id)
    import httpx
    async with httpx.AsyncClient(
        base_url=reacher.REACHER_BASE_URL, timeout=30.0, headers=headers,
    ) as client:
        creators_task = reacher.get_product_creators(
            product_id, page_size=cpp, client=client,
        )
        videos_task = reacher.get_product_videos(
            product_id, page_size=videos_per_product,
            time_range="30 days", client=client,
        )
        results = await asyncio.gather(
            creators_task, videos_task, return_exceptions=True,
        )
        creators_res, videos_res = results
    return {
        "competitor_count": 1,
        "competitors": [{
            "product": {"product_id": product_id, "_note": "supplied by caller"},
            "creators": (
                {"error": str(creators_res)}
                if isinstance(creators_res, Exception)
                else reacher._extract_items(creators_res)
            ),
            "videos": (
                {"error": str(videos_res)}
                if isinstance(videos_res, Exception)
                else reacher._extract_items(videos_res)
            ),
        }],
        "source": "explicit_product_id",
    }


async def gather_competitor_intel(
    query: str,
    *,
    top_products: int = 3,
    creators_per_product: int | None = None,
    shop_id: str | None = None,
) -> dict[str, Any]:
    """Subagent 1: products → creators. Retries with a broader query on 0 hits."""
    cpp = creators_per_product or _default_creators_per_product()
    landscape = await reacher.get_competitor_landscape(
        query,
        top_products=top_products,
        creators_per_product=cpp,
        videos_per_product=4,
        time_range="30 days",
        shop_id=shop_id,
    )
    if landscape.get("competitor_count", 0) > 0:
        return landscape
    fallback = _broaden(query)
    if not fallback:
        return landscape
    landscape2 = await reacher.get_competitor_landscape(
        fallback,
        top_products=top_products,
        creators_per_product=cpp,
        videos_per_product=4,
        time_range="30 days",
        shop_id=shop_id,
    )
    landscape2["fallback_query_used"] = fallback
    landscape2["original_query"] = query
    return landscape2


async def gather_trending_hooks(
    query: str, *, page_size: int = 20, shop_id: str | None = None,
) -> dict[str, Any]:
    """Subagent 2: trending videos → hook metadata. Retries broader on 0 hits."""

    async def _fetch(q: str) -> list[dict[str, Any]]:
        payload = await reacher.get_trending_videos(
            q, page_size=page_size, sort_by="views", time_range="7 days",
            shop_id=shop_id,
        )
        return reacher._extract_items(payload)

    items = await _fetch(query)
    used = query
    if not items:
        fallback = _broaden(query)
        if fallback:
            items = await _fetch(fallback)
            used = fallback
    out: dict[str, Any] = {
        "query": used,
        "hooks": [_summarize_video_for_hook(v) for v in items[:15]],
    }
    if used != query:
        out["original_query"] = query
        out["fallback_query_used"] = used
    return out


def gather_company_context(*, max_past: int = 8, per_campaign_chars: int = 1200) -> dict[str, Any]:
    """Subagent 3: brand context + compressed history of past campaigns.

    Reads every file listed in `COMPANY_CONTEXT_FILES` from the local `data/`
    folder. Past campaigns are written by `persist_campaign()` and synced to
    Nia, so over time both the local files AND the Nia index see the same
    compressed history.
    """
    company_docs: dict[str, str] = {}
    for filename in COMPANY_CONTEXT_FILES:
        path = DATA_DIR / filename
        if path.exists():
            company_docs[filename] = path.read_text(encoding="utf-8")

    past: list[dict[str, str]] = []
    if CAMPAIGNS_DIR.exists():
        files = sorted(
            CAMPAIGNS_DIR.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in files[:max_past]:
            text = p.read_text(encoding="utf-8")
            note = _extract_memory_note(text) or text[:per_campaign_chars]
            past.append({"campaign": p.stem, "memory_note": note})
    return {"company_docs": company_docs, "past_campaign_memory": past}


def _extract_memory_note(markdown: str) -> str | None:
    """Pull just the `## Memory note` paragraph out of a saved campaign file."""
    m = re.search(
        r"^##\s+Memory note\s*\n(.+?)(?:\n##\s|\Z)",
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
    return m.group(1).strip() if m else None


async def generate_campaign(
    brief: str,
    intel: dict[str, Any] | Exception,
    hooks: dict[str, Any] | Exception,
    context: dict[str, Any],
    pulse: dict[str, Any] | Exception | None = None,
) -> str:
    payload: dict[str, Any] = {
        "brief": brief,
        "competitor_intel": (
            {"error": str(intel)} if isinstance(intel, Exception) else intel
        ),
        "trending_hooks": (
            {"error": str(hooks)} if isinstance(hooks, Exception) else hooks
        ),
        "company_context": context,
    }
    if pulse is not None:
        payload["social_pulse"] = (
            {"error": str(pulse)} if isinstance(pulse, Exception) else pulse
        )
    user_msg = json.dumps(payload, default=str)[:60_000]
    return await agent.chat_completion(
        messages=[
            {"role": "system", "content": CAMPAIGN_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=2000,
        temperature=0.7,
    )


def _slugify(text: str, max_len: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "campaign")[:max_len]


def persist_campaign(brief: str, query: str, campaign_md: str) -> Path:
    """Write the campaign to `data/campaigns/` and trigger a Nia sync."""
    CAMPAIGNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = CAMPAIGNS_DIR / f"{ts}-{_slugify(query or brief)}.md"
    header = (
        f"<!-- generated_at: {ts} -->\n"
        f"<!-- brief: {brief} -->\n"
        f"<!-- product_query: {query} -->\n\n"
    )
    path.write_text(header + campaign_md.strip() + "\n", encoding="utf-8")
    _trigger_nia_sync()
    return path


def _trigger_nia_sync() -> None:
    """Fire-and-forget `nia local sync` so future runs see this campaign."""
    try:
        subprocess.Popen(
            [NIA, "local", "sync"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.warning("nia CLI not found on PATH; skipping local sync")
    except Exception as exc:
        logger.warning("nia local sync failed to launch: %s", exc)


async def run_campaign_pipeline(
    brief: str,
    *,
    product_id: str | None = None,
    shop_id: str | None = None,
    include_social_pulse: bool = False,
    social_platforms: list[str] | None = None,
    publish_scripts: bool = False,
    scripts_count: int = 3,
    scripts_page_id: str | None = None,
    brand_name: str = "Aroma Cloud",
) -> dict[str, Any]:
    """Orchestrator entry point — used by `POST /api/marketing-campaign`.

    If `product_id` is supplied, subagent 1 skips the catalog search and
    pulls creators + videos directly for that product. Subagent 2 (trending
    hooks) still uses the LLM-extracted query to keep the hook signal broad.

    `shop_id` overrides the `REACHER_SHOP_ID` env for this run only.

    `include_social_pulse=True` adds a 4th subagent that runs the Browser-Use
    scrapers (TikTok / X / Reddit / LinkedIn) in parallel. Adds 30–90s of
    latency per platform — opt-in only. `social_platforms` selects a subset
    (defaults to twitter+reddit+linkedin; pass `["tiktok"]` etc. to narrow).
    """
    query = await extract_product_query(brief)

    if product_id:
        intel_task = asyncio.create_task(
            gather_competitor_intel_for_product_id(product_id, shop_id=shop_id)
        )
    else:
        intel_task = asyncio.create_task(
            gather_competitor_intel(query, shop_id=shop_id)
        )
    hooks_task = asyncio.create_task(
        gather_trending_hooks(query, shop_id=shop_id)
    )
    context = gather_company_context()

    pulse_task: asyncio.Task | None = None
    if include_social_pulse:
        pulse_task = asyncio.create_task(
            social_pulse.gather_social_pulse(query, platforms=social_platforms)
        )

    parallel = [intel_task, hooks_task]
    if pulse_task is not None:
        parallel.append(pulse_task)
    gathered = await asyncio.gather(*parallel, return_exceptions=True)
    intel, hooks = gathered[0], gathered[1]
    pulse = gathered[2] if pulse_task is not None else None

    campaign_md = await generate_campaign(brief, intel, hooks, context, pulse)
    saved_to = persist_campaign(brief, query, campaign_md)

    result: dict[str, Any] = {
        "brief": brief,
        "extracted_query": query,
        "campaign_markdown": campaign_md,
        "memory_note": _extract_memory_note(campaign_md),
        "saved_to": str(saved_to),
        "subagents": {
            "competitor_intel": (
                {"error": str(intel)} if isinstance(intel, Exception) else intel
            ),
            "trending_hooks": (
                {"error": str(hooks)} if isinstance(hooks, Exception) else hooks
            ),
            "company_context": {
                "loaded_files": list((context.get("company_docs") or {}).keys()),
                "past_campaign_count": len(context.get("past_campaign_memory") or []),
            },
            **(
                {"social_pulse": (
                    {"error": str(pulse)} if isinstance(pulse, Exception) else pulse
                )}
                if pulse_task is not None else {}
            ),
        },
    }

    # Post-campaign creator-script step. Always builds (so the response shows
    # what WOULD be published), but only PATCHes Notion when publish_scripts
    # is true AND NOTION_API_KEY + NOTION_SCRIPTS_PAGE_ID resolve.
    try:
        result["scripts"] = await scripts.propose_scripts_for_campaign(
            brief=brief,
            intel=intel,
            hooks=hooks,
            context=context,
            pulse=pulse,
            campaign_markdown=campaign_md,
            brand_name=brand_name,
            count=scripts_count,
            publish=publish_scripts,
            page_id=scripts_page_id,
        )
    except Exception as exc:
        logger.warning("scripts step failed: %s", exc)
        result["scripts"] = {"error": str(exc)}

    # Post-campaign automation step. Always runs (so the response shows what
    # WOULD be sent), but the actual POST to Reacher is gated by
    # AUTOMATIONS_ENABLED + AUTOMATIONS_DRY_RUN inside automations.py.
    try:
        result["automations"] = await automations.propose_automations_for_campaign(
            result, shop_id=shop_id,
        )
    except Exception as exc:
        logger.warning("automations step failed: %s", exc)
        result["automations"] = {"error": str(exc)}

    return result
