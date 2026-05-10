"""Creator-script generator + Notion publisher.

Post-synthesis subagent. Takes the campaign's input signals
(competitor intel, trending hooks, social pulse, brand context) and
produces N short-form video scripts ready to hand to creators. Optionally
appends them to a Notion page.

Two layers:

  1. `generate_scripts(...)`              — LLM call. Returns list[dict].
  2. `publish_to_notion(blocks, ...)`     — REST call to Notion. Side-effect.

The high-level entry point `propose_scripts_for_campaign(...)` wires both
together and is invoked by `campaign.run_campaign_pipeline` after the
campaign markdown is persisted.

Notion setup (one-time, per workspace):
  1. Create an internal integration at https://www.notion.so/my-integrations
  2. Copy its "Internal Integration Token" -> NOTION_API_KEY env var
  3. Open the target Scripts page in Notion, click "..." -> "Add connections"
     and grant the integration access. Without this step every API call 404s.
  4. Set NOTION_SCRIPTS_PAGE_ID to the page ID (32 hex chars from the URL,
     with or without dashes — both work).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

import agent

logger = logging.getLogger("uvicorn.error")

NOTION_API_BASE = "https://api.notion.com/v1"
# Pinned per https://developers.notion.com/reference/versioning. Bump
# carefully — block schemas change between major versions.
NOTION_VERSION = "2022-06-28"
# Notion enforces 100 children per PATCH /blocks/{id}/children.
NOTION_BLOCK_CHUNK = 100


SCRIPTS_SYSTEM_PROMPT = """You are a senior short-form video scriptwriter.

You will receive a JSON payload with:
  - brief             : the team's request
  - brand_name        : the brand commissioning the work
  - count             : how many scripts to produce
  - competitor_intel  : similar TikTok Shop products + the creators driving them
  - trending_hooks    : top-performing videos in the same product space
  - social_pulse      : OPTIONAL. Recent posts about the topic across X,
                        Reddit, LinkedIn, TikTok. Use for live cultural context.
  - company_context   : brand voice + past-campaign memory notes
  - campaign_markdown : the synthesized campaign (use it for hooks + creator picks)

Produce EXACTLY `count` short-form video scripts. Return STRICTLY a JSON
object of the form:

  {
    "scripts": [
      {
        "title": "<4-7 words, distinctive>",
        "creator": "<@handle from competitor_intel.competitors[*].creators[*]
                    or social_pulse.results[*].items[*].handle, otherwise 'TBD'>",
        "platform": "TikTok" | "Instagram Reels" | "YouTube Shorts",
        "duration_seconds": 15 | 30 | 45 | 60,
        "hook": "<opening line, <=12 words, must grab attention in <2s>",
        "beats": [
          {"seconds": <int 0-N>, "visual": "<one short shot directive>",
           "voiceover": "<1 sentence VO or on-screen text>"}
        ],
        "outro": "<closing CTA, <=15 words>",
        "sourced_from": [
          "<short citation pointing to a real source, e.g.
            'twitter:@SamJWasserman', 'trending:lip-oil-glide',
            'reddit:r/SkincareAddiction'>"
        ]
      }
    ]
  }

Per-script requirements (HARD):
  * `beats` length: 4-6 entries that fit inside `duration_seconds`.
  * Reference at least 2 real phrases from `social_pulse` or `trending_hooks`.
    Quote them verbatim where possible; cite them in `sourced_from`.
  * If you use a `creator` handle, that handle MUST appear in the input
    payload (competitor_intel.creators OR social_pulse.results[*].items).
    If no real handle is available, use "TBD" — never invent one.
  * Tone must match the brand voice in `company_context.company_docs.brand-guide.md`.
    Obey every "No" / "Did not work" rule there. NEVER use "elevate", "unlock",
    "fuel", "game-changing", "liquid energy", or any pun on "brew/brewed/espress"
    beyond plain product use.
  * Do NOT reuse a hook across multiple scripts — diversify angle/format.

If a section of the input is empty or `{"error": ...}`, work around it
silently (don't apologize in the script). If both Reacher streams AND
social_pulse are empty, default to archetype-driven scripts and put "TBD"
in `creator`.

Return ONLY the JSON object. No prose before or after.
"""


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _slim_intel(intel: dict[str, Any] | None) -> dict[str, Any] | None:
    """Trim competitor_intel to the fields the writer actually needs."""
    if not isinstance(intel, dict):
        return intel
    out: dict[str, Any] = {}
    if intel.get("error"):
        return {"error": intel["error"]}
    competitors = []
    for c in intel.get("competitors") or []:
        creators = []
        for cr in (c.get("creators") or [])[:8]:
            creators.append({
                "handle": cr.get("handle") or cr.get("username"),
                "name": cr.get("name"),
                "followers": cr.get("followers"),
            })
        competitors.append({
            "product_title": (c.get("product") or {}).get("title"),
            "creators": creators,
        })
    out["competitors"] = competitors
    return out


def _slim_hooks(hooks: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(hooks, dict):
        return hooks
    if hooks.get("error"):
        return {"error": hooks["error"]}
    out_hooks = []
    for h in (hooks.get("hooks") or [])[:8]:
        out_hooks.append({
            "title": h.get("title"),
            "caption": h.get("caption"),
            "creator_handle": h.get("creator_handle"),
            "tags": h.get("content_tags") or h.get("ai_tags"),
        })
    return {"hooks": out_hooks}


def _slim_pulse(pulse: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop raw output, keep just the parsed items per platform."""
    if not isinstance(pulse, dict):
        return pulse
    if pulse.get("error"):
        return {"error": pulse["error"]}
    slim: dict[str, Any] = {"platforms": {}}
    for platform, res in (pulse.get("results") or {}).items():
        slim["platforms"][platform] = {
            "items": (res.get("items") or [])[:5],
            "success": res.get("success"),
        }
    return slim


async def generate_scripts(
    *,
    brief: str,
    intel: dict[str, Any] | Exception | None,
    hooks: dict[str, Any] | Exception | None,
    context: dict[str, Any],
    pulse: dict[str, Any] | Exception | None = None,
    campaign_markdown: str | None = None,
    brand_name: str = "Aroma Cloud",
    count: int = 3,
) -> list[dict[str, Any]]:
    """Run the LLM and return the parsed `scripts` list."""
    payload: dict[str, Any] = {
        "brief": brief,
        "brand_name": brand_name,
        "count": count,
        "competitor_intel": (
            {"error": str(intel)} if isinstance(intel, Exception) else _slim_intel(intel)
        ),
        "trending_hooks": (
            {"error": str(hooks)} if isinstance(hooks, Exception) else _slim_hooks(hooks)
        ),
        "company_context": {
            "company_docs": (context.get("company_docs") or {}),
            "past_campaign_memory": (context.get("past_campaign_memory") or [])[:3],
        },
        "campaign_markdown": campaign_markdown or "",
    }
    if pulse is not None:
        payload["social_pulse"] = (
            {"error": str(pulse)} if isinstance(pulse, Exception) else _slim_pulse(pulse)
        )

    raw = await agent.chat_completion(
        messages=[
            {"role": "system", "content": SCRIPTS_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, default=str)[:60_000]},
        ],
        max_tokens=2500,
        temperature=0.7,
        response_format={"type": "json_object"},
    )
    return _parse_scripts(raw)


def _parse_scripts(raw: str) -> list[dict[str, Any]]:
    """Tolerant parse of the LLM output. JSON-mode usually returns a clean
    object, but fall back to substring-match if it wraps in prose."""
    if not raw:
        return []
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        items = data.get("scripts") or data.get("items") or data.get("data") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    return [s for s in items if isinstance(s, dict)]


# ---------------------------------------------------------------------------
# Notion publishing
# ---------------------------------------------------------------------------


def _format_page_id(page_id: str) -> str:
    """Notion accepts both dashed and undashed; canonicalize to dashed UUID."""
    s = re.sub(r"-", "", page_id or "").strip()
    if len(s) != 32 or not re.fullmatch(r"[0-9a-fA-F]{32}", s):
        return page_id  # let Notion error
    return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"


def _rich(text: str, *, bold: bool = False, italic: bool = False,
          code: bool = False) -> dict:
    block: dict = {"type": "text", "text": {"content": text or ""}}
    annotations = {}
    if bold:
        annotations["bold"] = True
    if italic:
        annotations["italic"] = True
    if code:
        annotations["code"] = True
    if annotations:
        block["annotations"] = annotations
    return block


def _heading(level: int, text: str) -> dict:
    return {
        "object": "block",
        "type": f"heading_{level}",
        f"heading_{level}": {"rich_text": [_rich(text)]},
    }


def _paragraph(*runs: dict) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": list(runs) or [_rich("")]},
    }


def _numbered(text: str) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": [_rich(text)]},
    }


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _format_notion_blocks(
    scripts: list[dict[str, Any]], *, campaign_name: str, brand_name: str,
) -> list[dict]:
    """Assemble the block list to PATCH onto the Scripts page."""
    blocks: list[dict] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blocks.append(_heading(1, f"{brand_name} | {campaign_name}"))
    blocks.append(_paragraph(_rich(f"Generated: {ts}", italic=True)))
    blocks.append(_divider())

    for i, s in enumerate(scripts, start=1):
        title = (s.get("title") or "Untitled").strip() or "Untitled"
        creator = (s.get("creator") or "TBD").strip()
        platform = (s.get("platform") or "TikTok").strip()
        duration = s.get("duration_seconds") or 30
        hook = (s.get("hook") or "").strip()
        outro = (s.get("outro") or "").strip()
        beats = s.get("beats") or []
        sourced = s.get("sourced_from") or []

        blocks.append(_heading(2, f"{i}. {title}"))
        blocks.append(_paragraph(
            _rich("Creator: ", bold=True), _rich(f"{creator}   "),
            _rich("Platform: ", bold=True), _rich(f"{platform}   "),
            _rich("Duration: ", bold=True), _rich(f"{duration}s"),
        ))

        blocks.append(_heading(3, "Hook"))
        blocks.append(_paragraph(_rich(hook)))

        blocks.append(_heading(3, "Beats"))
        for beat in beats:
            if not isinstance(beat, dict):
                continue
            sec = beat.get("seconds")
            visual = (beat.get("visual") or "").strip()
            vo = (beat.get("voiceover") or "").strip()
            t = f"[{sec}s] {visual}"
            if vo:
                t += f"   |   VO: {vo}"
            blocks.append(_numbered(t))

        blocks.append(_heading(3, "Outro"))
        blocks.append(_paragraph(_rich(outro)))

        if sourced:
            blocks.append(_paragraph(
                _rich("Sourced from: ", italic=True, bold=True),
                _rich(", ".join(str(x) for x in sourced), italic=True),
            ))
        blocks.append(_divider())
    return blocks


_MD_BOLD_RE = re.compile(r"\*\*([^*]+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_MD_CODE_RE = re.compile(r"`([^`\n]+?)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def _md_inline_to_rich(text: str) -> list[dict]:
    """Parse a single line's inline markdown into Notion rich_text runs.
    Handles **bold**, *italic*, `code`, and [text](url) links. Non-overlapping.
    """
    if not text:
        return [_rich("")]

    # Tokenize into spans without overlapping. Bold first (greediest), then
    # links, then italic, then code.
    spans: list[tuple[int, int, str, Any]] = []
    used: list[tuple[int, int]] = []

    def overlaps(s: int, e: int) -> bool:
        return any(not (e <= us or s >= ue) for us, ue in used)

    for m in _MD_BOLD_RE.finditer(text):
        if not overlaps(m.start(), m.end()):
            spans.append((m.start(), m.end(), "bold", m.group(1)))
            used.append((m.start(), m.end()))
    for m in _MD_LINK_RE.finditer(text):
        if not overlaps(m.start(), m.end()):
            spans.append((m.start(), m.end(), "link", (m.group(1), m.group(2))))
            used.append((m.start(), m.end()))
    for m in _MD_ITALIC_RE.finditer(text):
        if not overlaps(m.start(), m.end()):
            spans.append((m.start(), m.end(), "italic", m.group(1)))
            used.append((m.start(), m.end()))
    for m in _MD_CODE_RE.finditer(text):
        if not overlaps(m.start(), m.end()):
            spans.append((m.start(), m.end(), "code", m.group(1)))
            used.append((m.start(), m.end()))

    spans.sort(key=lambda x: x[0])
    runs: list[dict] = []
    cursor = 0
    for start, end, kind, content in spans:
        if start > cursor:
            runs.append(_rich(text[cursor:start]))
        if kind == "bold":
            runs.append(_rich(content, bold=True))
        elif kind == "italic":
            runs.append(_rich(content, italic=True))
        elif kind == "code":
            runs.append(_rich(content, code=True))
        elif kind == "link":
            label, url = content
            runs.append({
                "type": "text",
                "text": {"content": label, "link": {"url": url}},
            })
        cursor = end
    if cursor < len(text):
        runs.append(_rich(text[cursor:]))
    return runs or [_rich(text)]


def markdown_to_notion_blocks(md: str) -> list[dict]:
    """Convert simple markdown to Notion blocks. Handles headings #/##/###,
    bullets (- / *), numbered (1. 2.), and paragraphs. Skips tables and
    nested lists (the campaign markdown doesn't use them)."""
    blocks: list[dict] = []
    if not md:
        return blocks
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.lstrip()
        if not stripped:
            i += 1
            continue

        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            blocks.append({
                "object": "block",
                "type": f"heading_{level}",
                f"heading_{level}": {"rich_text": _md_inline_to_rich(m.group(2))},
            })
            i += 1
            continue

        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _md_inline_to_rich(m.group(1))},
            })
            i += 1
            continue

        m = re.match(r"^\d+\.\s+(.*)$", stripped)
        if m:
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _md_inline_to_rich(m.group(1))},
            })
            i += 1
            continue

        # Plain paragraph — accumulate adjacent non-blank, non-special lines.
        para_lines = [stripped]
        j = i + 1
        while j < len(lines):
            nxt = lines[j].rstrip().lstrip()
            if not nxt:
                break
            if (re.match(r"^(#{1,3})\s+", nxt)
                    or re.match(r"^[-*]\s+", nxt)
                    or re.match(r"^\d+\.\s+", nxt)):
                break
            para_lines.append(nxt)
            j += 1
        para_text = " ".join(para_lines)
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _md_inline_to_rich(para_text)},
        })
        i = j

    return blocks


async def create_campaign_page(
    *,
    parent_page_id: str,
    title: str,
    campaign_md: str,
    scripts_list: list[dict[str, Any]],
    brand_name: str,
    api_key: str,
) -> dict[str, Any]:
    """Create a NEW child page under `parent_page_id` containing the campaign
    strategy followed by the creator scripts in a single document.

    Returns `{page_id, page_url, appended_block_count}`. Notion limits page-create
    payloads to 100 children, so any overflow gets PATCHed in chunks afterwards.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    parent = _format_page_id(parent_page_id)

    # Build the unified block list: strategy first, then scripts.
    blocks: list[dict] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blocks.append(_paragraph(_rich(f"Generated: {ts}", italic=True)))
    blocks.append(_divider())
    blocks.append(_heading(1, "Strategy"))
    blocks.extend(markdown_to_notion_blocks(campaign_md))
    if scripts_list:
        blocks.append(_divider())
        # _format_notion_blocks already prepends its own h1+divider, so feed
        # it directly.
        blocks.extend(_format_notion_blocks(
            scripts_list, campaign_name="Creator scripts", brand_name=brand_name,
        ))

    page_title = f"{brand_name} | {title}"
    initial = blocks[:NOTION_BLOCK_CHUNK]
    overflow = blocks[NOTION_BLOCK_CHUNK:]

    body: dict[str, Any] = {
        "parent": {"page_id": parent},
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": page_title}}]},
        },
        "children": initial,
    }

    async with httpx.AsyncClient(
        base_url=NOTION_API_BASE, timeout=30.0, headers=headers,
    ) as client:
        resp = await client.post("/pages", json=body)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            raise RuntimeError(f"Notion {resp.status_code} on /pages: {data}")
        page_id = data.get("id") or ""
        page_url = data.get("url") or (
            f"https://www.notion.so/{page_id.replace('-', '')}" if page_id else ""
        )
        appended = len(initial)

        if overflow and page_id:
            for start in range(0, len(overflow), NOTION_BLOCK_CHUNK):
                chunk = overflow[start:start + NOTION_BLOCK_CHUNK]
                r = await client.patch(
                    f"/blocks/{_format_page_id(page_id)}/children",
                    json={"children": chunk},
                )
                try:
                    rb = r.json()
                except Exception:
                    rb = {"raw": r.text}
                if r.status_code >= 400:
                    raise RuntimeError(
                        f"Notion {r.status_code} on /blocks/{page_id}/children: {rb}"
                    )
                appended += len(rb.get("results") or [])

    return {
        "page_id": page_id,
        "page_url": page_url,
        "appended_block_count": appended,
    }


async def publish_to_notion(
    blocks: list[dict], *, page_id: str, api_key: str,
) -> dict[str, Any]:
    """PATCH /blocks/{page}/children, chunked at 100 per call."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    page = _format_page_id(page_id)
    appended = 0
    responses: list[dict] = []
    async with httpx.AsyncClient(
        base_url=NOTION_API_BASE, timeout=30.0, headers=headers,
    ) as client:
        for start in range(0, len(blocks), NOTION_BLOCK_CHUNK):
            chunk = blocks[start:start + NOTION_BLOCK_CHUNK]
            resp = await client.patch(
                f"/blocks/{page}/children", json={"children": chunk},
            )
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text}
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Notion {resp.status_code} on /blocks/{page}/children: {body}"
                )
            appended += len(body.get("results") or [])
            responses.append(body)
    return {
        "appended_block_count": appended,
        "page_id": page,
        "page_url": f"https://www.notion.so/{page.replace('-', '')}",
    }


# ---------------------------------------------------------------------------
# High-level: post-campaign hook
# ---------------------------------------------------------------------------


def _campaign_name_from_md(campaign_md: str) -> str:
    m = re.search(r"^#\s+Campaign:\s+(.+?)\s*$", campaign_md, flags=re.MULTILINE)
    return (m.group(1).strip() if m else "Untitled Campaign")[:120]


async def propose_scripts_for_campaign(
    *,
    brief: str,
    intel: dict[str, Any] | Exception | None,
    hooks: dict[str, Any] | Exception | None,
    context: dict[str, Any],
    pulse: dict[str, Any] | Exception | None = None,
    campaign_markdown: str = "",
    brand_name: str = "Aroma Cloud",
    count: int = 3,
    publish: bool = False,
    page_id: str | None = None,
) -> dict[str, Any]:
    """Generate + (optionally) publish creator scripts.

    Returns a single dict with:
      - `scripts`         : the parsed LLM output (list[dict])
      - `notion`          : publish result, or skip-reason, or error
      - `published`       : bool — True iff the Notion PATCH succeeded
    """
    try:
        scripts = await generate_scripts(
            brief=brief,
            intel=intel,
            hooks=hooks,
            context=context,
            pulse=pulse,
            campaign_markdown=campaign_markdown,
            brand_name=brand_name,
            count=count,
        )
    except Exception as exc:
        logger.warning("scripts generation failed: %s", exc)
        return {
            "scripts": [],
            "notion": {"skipped": True, "reason": "generation_failed"},
            "published": False,
            "error": str(exc),
        }

    if not scripts:
        return {
            "scripts": [],
            "notion": {"skipped": True, "reason": "no_scripts_returned"},
            "published": False,
        }

    if not publish:
        return {
            "scripts": scripts,
            "notion": {"skipped": True, "reason": "publish_disabled"},
            "published": False,
        }

    api_key = os.environ.get("NOTION_API_KEY")
    parent_page_id = page_id or os.environ.get("NOTION_SCRIPTS_PAGE_ID")
    if not api_key or not parent_page_id:
        return {
            "scripts": scripts,
            "notion": {
                "skipped": True,
                "reason": "missing NOTION_API_KEY and/or NOTION_SCRIPTS_PAGE_ID",
            },
            "published": False,
        }

    campaign_name = _campaign_name_from_md(campaign_markdown)
    try:
        notion_result = await create_campaign_page(
            parent_page_id=parent_page_id,
            title=campaign_name,
            campaign_md=campaign_markdown,
            scripts_list=scripts,
            brand_name=brand_name,
            api_key=api_key,
        )
    except Exception as exc:
        logger.warning("Notion publish failed: %s", exc)
        return {
            "scripts": scripts,
            "notion": {"skipped": False, "error": str(exc)},
            "published": False,
        }

    return {
        "scripts": scripts,
        "notion": notion_result,
        "published": True,
    }


__all__ = [
    "NOTION_API_BASE",
    "NOTION_VERSION",
    "SCRIPTS_SYSTEM_PROMPT",
    "create_campaign_page",
    "generate_scripts",
    "markdown_to_notion_blocks",
    "propose_scripts_for_campaign",
    "publish_to_notion",
]
