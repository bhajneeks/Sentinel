"""OpenAI tool layer.

Rachel (the chat agent) drives TWO distinct pipelines from the same iMessage
conversation. The intent routing lives in `prompt.md`; this module owns the
machinery.

PIPELINE A — TRACKING (live company-watching):
- `track_company(company, link)`  — open a fresh tracking run + spawn 4
                                    supervised browser agents AND persist a
                                    Nia-indexed record under `data/tracked/`.
- `search_reddit(query)`          — Reddit JSON-API scraper, fire-and-forget.
- `search_x(query)`               — browser-use scraper for X (counts toward cap).
- `search_linkedin(query)`        — browser-use scraper for LinkedIn (counts toward cap).
- `screenshot(platform)`          — peek at what a supervised agent is seeing.
- `redirect(platform, task)`      — steer a supervised agent to a new task.
- `close(platform)` / `spawn(...)`— manage the supervised agents.
- `list_tracked_topics()`         — answer "what topics are you watching?".

PIPELINE B — MARKETING CAMPAIGN (one-shot):
- `create_marketing_campaign(brief, ...)` — invoke `campaign.run_campaign_pipeline`,
                                    which fans out competitor intel + trending
                                    hooks + (optional) social pulse + brand
                                    context, synthesizes a campaign markdown,
                                    persists it under `data/campaigns/`, and
                                    fires DM-automation proposals.

Tools return short status strings so the LLM can narrate. Heavy work runs in
background tasks that stream events + mentions to Convex; the frontend
subscribes and injects findings into the chat as Rachel's own messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import campaign
import convex_client as cx
import scraper_stream
import supervised_agent

logger = logging.getLogger("uvicorn.error")

# participant -> active agentRuns _id (Convex doc id)
_active_run_by_participant: dict[str, str] = {}
# participant -> active company name (for note_user_comment defaulting)
_active_company_by_participant: dict[str, str] = {}

# Local persistence for tracked companies. Mirrors campaign.CAMPAIGNS_DIR
# layout. `nia local sync` (fired via campaign._trigger_nia_sync) picks it up.
ROOT = Path(__file__).resolve().parent
DATA_DIR = (ROOT / ".." / "data").resolve()
TRACKED_DIR = DATA_DIR / "tracked"


def _slugify(text: str, max_len: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "tracked")[:max_len]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Tracking-file editing helpers ─────────────────────────────────────────────
#
# Files live at `data/tracked/{slug}.md` (one per company). Re-tracking the
# same company UPSERTS into the existing file rather than creating a new one.
# Body sections accumulate over time:
#   ## Sources       — every URL the user has supplied for this company
#   ## Runs          — audit trail of when tracking was fired + by whom
#   ## User comments — opinions / takes / context the user has shared
#
# Frontmatter is HTML comments at the top: company, slug, first_tracked,
# last_tracked, runs (count), last_comment_at. Update via _set_frontmatter.


_FRONTMATTER_RE = re.compile(r"^<!--\s*(\w+):\s*(.*?)\s*-->", re.MULTILINE)


def _set_frontmatter(text: str, key: str, value: str) -> str:
    """Update a `<!-- key: value -->` line, or insert into the frontmatter block."""
    pattern = re.compile(rf"<!--\s*{re.escape(key)}:\s*[^>]*?\s*-->")
    repl = f"<!-- {key}: {value} -->"
    if pattern.search(text):
        return pattern.sub(repl, text, count=1)
    # Append to the end of the leading frontmatter block.
    m = re.match(r"\A(?:<!--[^\n]*-->\n)+", text)
    if m:
        return text[:m.end()] + repl + "\n" + text[m.end():]
    return repl + "\n" + text


def _bump_frontmatter_int(text: str, key: str) -> str:
    pattern = re.compile(rf"<!--\s*{re.escape(key)}:\s*(\d+)\s*-->")
    m = pattern.search(text)
    if m:
        bumped = int(m.group(1)) + 1
        return pattern.sub(f"<!-- {key}: {bumped} -->", text, count=1)
    return _set_frontmatter(text, key, "1")


def _append_to_section(text: str, section_name: str, line: str) -> str:
    """Append `line` to `## section_name`. Section is created at end if absent."""
    line = line.rstrip()
    header = f"## {section_name}"
    if header not in text:
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + sep + f"{header}\n{line}\n"
    start = text.index(header) + len(header)
    rest = text[start:]
    nxt = rest.find("\n## ")
    end = len(text) if nxt == -1 else start + nxt
    section_body = text[start:end].rstrip("\n").rstrip()
    new_body = f"{section_body}\n{line}\n" if section_body else f"\n{line}\n"
    return text[:start] + new_body + text[end:]


def _new_tracked_file_text(
    *, company: str, slug: str, link: str, run_id: str,
    participant: str, now: str,
) -> str:
    return (
        f"<!-- company: {company} -->\n"
        f"<!-- slug: {slug} -->\n"
        f"<!-- first_tracked: {now} -->\n"
        f"<!-- last_tracked: {now} -->\n"
        f"<!-- runs: 1 -->\n\n"
        f"# Tracking: {company}\n\n"
        f"## Sources\n- {link}\n\n"
        f"## Runs\n- {now} — run_id={run_id}, by {participant}\n\n"
        f"## User comments\n"
    )


def _persist_tracked_company(
    *, company: str, link: str, run_id: str, participant: str,
) -> Path:
    """Upsert: edit `data/tracked/{slug}.md` for this company, or create it.

    Re-tracking the same company NEVER creates a duplicate file — it
    appends a new entry under `## Runs`, adds the URL to `## Sources` if
    it's new, and bumps last_tracked + runs in frontmatter.
    """
    TRACKED_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(company)
    path = TRACKED_DIR / f"{slug}.md"
    now = _now_iso()

    if path.exists():
        text = path.read_text(encoding="utf-8")
        text = _set_frontmatter(text, "last_tracked", now)
        text = _bump_frontmatter_int(text, "runs")
        text = _append_to_section(
            text, "Runs", f"- {now} — run_id={run_id}, by {participant}",
        )
        if link and f"- {link}" not in text:
            text = _append_to_section(text, "Sources", f"- {link}")
    else:
        text = _new_tracked_file_text(
            company=company, slug=slug, link=link,
            run_id=run_id, participant=participant, now=now,
        )

    path.write_text(text, encoding="utf-8")
    campaign._trigger_nia_sync()
    return path


def _append_user_comment(
    *, company: str, comment: str, participant: str,
) -> Path | None:
    """Append a `## User comments` line to the company's tracking file.

    Returns the file path on success, or None if no tracking file exists for
    the company yet (caller decides whether to seed one).
    """
    slug = _slugify(company)
    path = TRACKED_DIR / f"{slug}.md"
    if not path.exists():
        return None
    now = _now_iso()
    safe = comment.strip().replace("\n", " ")
    line = f'- {now} — "{safe}" (by {participant})'
    text = path.read_text(encoding="utf-8")
    text = _append_to_section(text, "User comments", line)
    text = _set_frontmatter(text, "last_comment_at", now)
    path.write_text(text, encoding="utf-8")
    campaign._trigger_nia_sync()
    return path


def _summarize_tracked_record(path: Path) -> dict[str, Any]:
    """Parse a tracked file into a compact summary for list_tracked_topics."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"file": path.name, "error": "unreadable"}
    meta: dict[str, str] = {}
    for k, v in _FRONTMATTER_RE.findall(text):
        meta[k] = v
    # First URL = first bullet under ## Sources.
    first_url = ""
    src = re.search(r"^##\s+Sources\s*\n((?:.+\n?)+?)(?=\n##\s|\Z)", text, re.MULTILINE)
    if src:
        m = re.search(r"^-\s*(\S+)", src.group(1), re.MULTILINE)
        if m:
            first_url = m.group(1).strip()
    # Comment count = number of "- " lines under ## User comments.
    comment_count = 0
    cmt = re.search(r"^##\s+User comments\s*\n((?:.+\n?)*?)(?=\n##\s|\Z)", text, re.MULTILINE)
    if cmt:
        comment_count = sum(1 for ln in cmt.group(1).splitlines() if ln.strip().startswith("-"))
    return {
        "file": path.name,
        "company": meta.get("company", ""),
        "slug": meta.get("slug", ""),
        "first_url": first_url,
        "first_tracked": meta.get("first_tracked", ""),
        "last_tracked": meta.get("last_tracked", ""),
        "runs": int(meta.get("runs", "1") or "1"),
        "user_comment_count": comment_count,
        "last_comment_at": meta.get("last_comment_at", ""),
    }


def _read_tracked_records(*, max_records: int = 25) -> list[dict[str, Any]]:
    """Scan `data/tracked/*.md`. Most-recently-tracked first."""
    if not TRACKED_DIR.exists():
        return []
    files = sorted(
        TRACKED_DIR.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_records]
    return [_summarize_tracked_record(p) for p in files]


def get_active_run(participant: str) -> str | None:
    return _active_run_by_participant.get(participant)


# ── tool schemas ──────────────────────────────────────────────────────────────


TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "track_company",
            "description": (
                "Open a tracking run for the current conversation. Call this "
                "as soon as you have BOTH a company name AND a link from the "
                "user. After this returns, you can call search_reddit / "
                "search_x / search_linkedin to actually pull mentions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "The company name to track.",
                    },
                    "link": {
                        "type": "string",
                        "description": "Any URL the user gave (homepage, social, news, etc.).",
                    },
                },
                "required": ["company", "link"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_reddit",
            "description": (
                "Search Reddit for posts and comments mentioning a query. "
                "Returns immediately; mentions stream into the UI as they're "
                "found. Requires a prior track_company call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search term (e.g. the company name or a related phrase).",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_x",
            "description": (
                "Open a real browser to X (Twitter) and pull live posts "
                "matching a query. Counts toward the 25-concurrent-browser "
                "cap. Returns immediately; mentions stream as they're found."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_linkedin",
            "description": (
                "Open a real browser to LinkedIn and pull recent posts "
                "matching a query. Counts toward the 25-concurrent-browser "
                "cap. Returns immediately; mentions stream as they're found."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": (
                "Get the latest step screenshot + state of a supervised "
                "browser agent (those auto-spawned by track_company). "
                "Pass `platform` (linkedin / x / reddit / tiktok) for the "
                "primary slot, or `linkedin@2` for an orbit instance. "
                "Returns task_status, current_url, step_summary, and a "
                "screenshot_url you can include in your reply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "description": "Platform name or instance id (e.g. 'linkedin' or 'linkedin@2').",
                    },
                },
                "required": ["platform"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "redirect",
            "description": (
                "Steer a running supervised browser agent to a new task "
                "without losing the session. Stops the current task, "
                "queues a new one on the same browser. Use this when "
                "you want the agent to look at something different on "
                "the same platform (e.g. 'now check the company's "
                "engineering jobs page' or 'search for layoffs')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "description": "Platform name or instance id (e.g. 'linkedin').",
                    },
                    "task": {
                        "type": "string",
                        "description": "Plain-English task instruction for the browser agent.",
                    },
                },
                "required": ["platform", "task"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close",
            "description": (
                "Stop a supervised browser agent. Frees a slot. If an "
                "orbit instance exists for that platform, it is promoted "
                "to fill the slot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "description": "Platform name or instance id.",
                    },
                },
                "required": ["platform"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn",
            "description": (
                "Open an ADDITIONAL supervised browser agent on a "
                "platform that already has one running. Useful when "
                "you want a parallel investigation on the same platform "
                "(e.g. one LinkedIn agent watching general posts, "
                "another reading the company page). Becomes an orbital "
                "node on the dashboard. Counts toward the 25-browser cap."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["linkedin", "x", "reddit", "tiktok"],
                    },
                    "task": {
                        "type": "string",
                        "description": "Optional plain-English task. If omitted, uses the platform default for the active company.",
                    },
                },
                "required": ["platform"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tracked_topics",
            "description": (
                "Returns the list of companies / topics the user has asked "
                "to track recently (most recent first). Use this when the "
                "user asks 'what are you tracking?', 'what topics are you "
                "watching?', 'what other things are you on?', etc. Reads "
                "from the local data/tracked/ folder which is mirrored "
                "into Nia. Returns JSON: {count, topics[{company, slug, "
                "first_url, first_tracked, last_tracked, runs, "
                "user_comment_count, last_comment_at}]}."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note_user_comment",
            "description": (
                "Save the user's opinion / take / reaction about a tracked "
                "company. Call this whenever the user expresses a "
                "subjective view, sentiment, prediction, or piece of "
                "context about a company they're tracking — even casual "
                "ones ('ngl their pricing is wild', 'i actually like the "
                "new model', 'wait that announcement was sus'). The "
                "comment is appended under '## User comments' in the "
                "company's tracking file so you and Nia can recall it "
                "later (e.g. 'u said earlier u didnt trust their "
                "privacy stuff'). Do NOT call for purely procedural "
                "messages ('yes', 'ok', 'thanks'), pasted links, or "
                "questions to you. Quietly save AND keep replying in "
                "your normal voice — never announce that you saved it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "comment": {
                        "type": "string",
                        "description": "The user's exact words or short paraphrase, <=240 chars.",
                    },
                    "company": {
                        "type": "string",
                        "description": (
                            "Optional company name. Defaults to the active "
                            "tracked company for this conversation."
                        ),
                    },
                },
                "required": ["comment"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_marketing_campaign",
            "description": (
                "DIFFERENT PIPELINE — call this when the user wants a "
                "MARKETING CAMPAIGN built (not live tracking). Triggers on "
                "phrases like 'make a campaign', 'create a marketing "
                "campaign', 'build me a campaign for my product', etc. "
                "Runs the full multi-subagent pipeline (Reacher competitor "
                "intel + trending hooks + brand context + optional social "
                "pulse), synthesizes a campaign markdown, persists it, and "
                "proposes DM automations + creator scripts. Takes 30-120s. "
                "Returns a JSON summary with campaign_name, saved_to, and "
                "which subagents ran."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "brief": {
                        "type": "string",
                        "description": (
                            "Free-text marketing brief, e.g. 'Make a campaign "
                            "for a tinted lip oil'. Should describe the product."
                        ),
                    },
                    "brand_name": {
                        "type": "string",
                        "description": "Brand commissioning the campaign. Defaults to 'Aroma Cloud'.",
                    },
                    "include_social_pulse": {
                        "type": "boolean",
                        "description": (
                            "Adds a 4th subagent that scrapes live posts from "
                            "TikTok / X / Reddit / LinkedIn. Adds 30-90s. Default false."
                        ),
                    },
                    "publish_scripts": {
                        "type": "boolean",
                        "description": (
                            "When true, publishes generated creator scripts to "
                            "the Notion Scripts page (NOTION_API_KEY required). "
                            "Default false."
                        ),
                    },
                },
                "required": ["brief"],
                "additionalProperties": False,
            },
        },
    },
]


# ── dispatch ──────────────────────────────────────────────────────────────────


async def dispatch(name: str, raw_args: str, *, participant: str) -> str:
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as exc:
        return f"error: bad JSON args ({exc})"

    handler = _HANDLERS.get(name)
    if handler is None:
        return f"error: unknown tool {name!r}"

    try:
        return await handler(args, participant=participant)
    except Exception as exc:  # surfaces back to the LLM
        logger.exception("tool %s crashed", name)
        return f"error: {exc}"


# ── handlers ──────────────────────────────────────────────────────────────────


async def _track_company(args: dict[str, Any], *, participant: str) -> str:
    company = (args.get("company") or "").strip()
    link = (args.get("link") or "").strip()
    if not company or not link:
        return "error: need both company and link"

    run_id = await cx.create_run(participant, company, link)
    _active_run_by_participant[participant] = run_id
    _active_company_by_participant[participant] = company

    # Upsert a Nia-indexed record. Re-tracking the same company never makes
    # a duplicate file — appends to ## Runs / ## Sources instead.
    try:
        saved_to = _persist_tracked_company(
            company=company, link=link, run_id=run_id, participant=participant,
        )
        logger.info("persisted tracking record: %s", saved_to)
    except Exception as exc:
        logger.warning("failed to persist tracking record: %s", exc)

    # Auto-spawn 4 supervised browser agents (one per platform) for this
    # company. Best-effort: cap-limited platforms get reported as skipped.
    # `run_id` is threaded through so the harvester can write mentions.
    spawn_results = await supervised_agent.spawn_company_agents(
        participant, company, run_id=run_id,
    )
    spawn_summary = ", ".join(f"{p}={s}" for p, s in spawn_results.items())

    return (
        f"started run {run_id} for {company} ({link}). "
        f"agents: {spawn_summary}. "
        "use screenshot / redirect / close / spawn to control them."
    )


async def _note_user_comment(
    args: dict[str, Any], *, participant: str,
) -> str:
    """Save a user opinion / reaction to the active tracked company's file."""
    comment = (args.get("comment") or "").strip()
    if not comment:
        return "error: comment is required"

    company = (args.get("company") or "").strip()
    if not company:
        company = _active_company_by_participant.get(participant, "")
    if not company:
        return "error: no active tracked company; pass company explicitly or call track_company first"

    try:
        path = _append_user_comment(
            company=company, comment=comment, participant=participant,
        )
    except Exception as exc:
        logger.exception("note_user_comment failed")
        return f"error: {exc}"

    if path is None:
        return f"error: no tracked file for {company}; call track_company first"
    return f"saved comment to {path.name}"


async def _list_tracked_topics(args: dict[str, Any], *, participant: str) -> str:
    """Return the recently-tracked companies, their first URL, and comment counts."""
    records = _read_tracked_records()
    if not records:
        return "no tracked topics yet"
    return json.dumps({"count": len(records), "topics": records})


async def _create_marketing_campaign(
    args: dict[str, Any], *, participant: str,
) -> str:
    """Run the full marketing-campaign pipeline. Returns short status for the LLM."""
    brief = (args.get("brief") or "").strip()
    if not brief:
        return "error: brief is required"

    # Optional knobs — default off for social_pulse (heavy), but default ON
    # for publish_scripts so every campaign produces a Notion page that
    # Rachel can text back as a single link.
    include_social_pulse = bool(args.get("include_social_pulse"))
    publish_scripts = bool(args.get("publish_scripts", True))
    brand_name = (args.get("brand_name") or "Aroma Cloud").strip() or "Aroma Cloud"

    try:
        result = await campaign.run_campaign_pipeline(
            brief,
            include_social_pulse=include_social_pulse,
            publish_scripts=publish_scripts,
            brand_name=brand_name,
        )
    except Exception as exc:
        logger.exception("create_marketing_campaign failed")
        return f"error: {exc}"

    # Pluck a short campaign name out of the markdown for the LLM to confirm.
    md = result.get("campaign_markdown") or ""
    name_match = re.search(r"^#\s+Campaign:\s+(.+?)\s*$", md, flags=re.MULTILINE)
    campaign_name = (name_match.group(1).strip() if name_match else "(untitled)")[:120]

    scripts_block = result.get("scripts") if isinstance(result.get("scripts"), dict) else {}
    notion_block = scripts_block.get("notion") if isinstance(scripts_block, dict) else {}
    notion_block = notion_block if isinstance(notion_block, dict) else {}

    summary = {
        "campaign_name": campaign_name,
        "extracted_query": result.get("extracted_query"),
        "saved_to": result.get("saved_to"),
        "subagents_run": list((result.get("subagents") or {}).keys()),
        # NEW: the URL to the Notion page containing campaign + scripts.
        # Rachel MUST include this URL verbatim in her reply to the user.
        "notion_page_url": notion_block.get("page_url"),
        "notion_published": bool(scripts_block.get("published")),
        "notion_skip_reason": notion_block.get("reason"),
        "notion_error": notion_block.get("error"),
        "automation_status": (
            (result.get("automations") or {}).get("dm", {}).get("status")
            if isinstance(result.get("automations"), dict) else None
        ),
    }
    return json.dumps(summary)


async def _screenshot(args: dict[str, Any], *, participant: str) -> str:
    platform = (args.get("platform") or "").strip()
    if not platform:
        return "error: platform is required"
    result = await supervised_agent.screenshot(participant, platform)
    return json.dumps(result)


async def _redirect(args: dict[str, Any], *, participant: str) -> str:
    platform = (args.get("platform") or "").strip()
    task = (args.get("task") or "").strip()
    if not platform or not task:
        return "error: platform and task are required"
    return await supervised_agent.redirect(participant, platform, task)


async def _close(args: dict[str, Any], *, participant: str) -> str:
    platform = (args.get("platform") or "").strip()
    if not platform:
        return "error: platform is required"
    return await supervised_agent.close(participant, platform)


async def _spawn(args: dict[str, Any], *, participant: str) -> str:
    platform = (args.get("platform") or "").strip()
    task = args.get("task")
    if not platform:
        return "error: platform is required"
    return await supervised_agent.spawn(
        participant,
        platform,
        task.strip() if isinstance(task, str) else None,
        run_id=_active_run_by_participant.get(participant),
    )


async def _search_platform(
    args: dict[str, Any],
    *,
    participant: str,
    platform: str,
    browser_backed: bool,
) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "error: query is required"

    run_id = _active_run_by_participant.get(participant)
    if not run_id:
        return "error: no active tracking run. call track_company first."

    if browser_backed:
        active = await cx.active_browser_count()
        if active >= cx.BROWSER_CONCURRENCY_CAP:
            return (
                f"error: 25-browser cap reached ({active} active). "
                "wait for some to finish, then retry."
            )

    session_id = await cx.start_session(
        run_id, platform, query, browser_backed=browser_backed
    )

    asyncio.create_task(
        scraper_stream.run_scraper(
            run_id=run_id,
            session_id=session_id,
            platform=platform,
            query=query,
        )
    )
    return f"started {platform} scrape for '{query}' (session {session_id})"


async def _search_reddit(args: dict[str, Any], *, participant: str) -> str:
    return await _search_platform(
        args, participant=participant, platform="reddit", browser_backed=False
    )


async def _search_x(args: dict[str, Any], *, participant: str) -> str:
    return await _search_platform(
        args, participant=participant, platform="x", browser_backed=True
    )


async def _search_linkedin(args: dict[str, Any], *, participant: str) -> str:
    return await _search_platform(
        args, participant=participant, platform="linkedin", browser_backed=True
    )


_HANDLERS = {
    # Tracking pipeline
    "track_company": _track_company,
    "search_reddit": _search_reddit,
    "search_x": _search_x,
    "search_linkedin": _search_linkedin,
    "screenshot": _screenshot,
    "redirect": _redirect,
    "close": _close,
    "spawn": _spawn,
    "list_tracked_topics": _list_tracked_topics,
    "note_user_comment": _note_user_comment,
    # Marketing campaign pipeline
    "create_marketing_campaign": _create_marketing_campaign,
}
