# Agentic pipeline

How the marketing-campaign + social-insights orchestrators are wired.
Companion to [`how-to-run-nozomio.md`](./how-to-run-nozomio.md) (ops),
[`how-to-run-reacher.md`](./how-to-run-reacher.md) (Reacher reference), and
[`how-to-run-tensorlake-photon.md`](./how-to-run-tensorlake-photon.md)
(scheduled-pulse cron). This doc is the *architecture* view: what calls
what, where the LLM enters and leaves, and where state lives between runs.

There are two top-level entry points:

| Endpoint | Purpose | Latency | LLM calls |
|---|---|---|---|
| `POST /api/marketing-campaign` | Full strategy + creator outreach + Notion scripts | ~30–120s | 3 (planner + synthesizer + scripts) |
| `POST /api/social-insights` | Quick "what's happening now" report | ~30–90s | 1 (summarizer only) |

The campaign endpoint can opt-in to social pulse as a 4th subagent — the
two paths share the Browser-Use scrapers but compose them differently.

---

## 1. One-screen mental model — campaign path

```
                          POST /api/marketing-campaign
                          { brief, product_id?, shop_id?,
                            include_social_pulse?, social_platforms? }
                                       │
                                       ▼
                       campaign.run_campaign_pipeline()
                                       │
        ┌──────────────────────────────┴──────────────────────────────┐
        ▼                                                              ▼
  agent.chat_completion (json_mode)                       gather_company_context()
  extract_product_query(brief)                            (sync disk reads)
        │                                                              │
        │  query = e.g. "tinted lip gloss"                              │
        ▼                                                              │
  asyncio.gather(                                                     │
    ┌─ Subagent 1 ──────────────────┐                                 │
    │ gather_competitor_intel()     │                                 │
    │   reacher.search_products()   │  ←─ retry with broader query    │
    │   reacher.get_product_         │     if 0 hits                   │
    │     creators() (per product)  │                                 │
    │   reacher.get_product_videos()│                                 │
    └───────────────────────────────┘                                 │
    ┌─ Subagent 2 ──────────────────┐                                 │
    │ gather_trending_hooks()       │                                 │
    │   reacher.get_trending_videos │                                 │
    │   keep only hook-relevant     │                                 │
    │   fields                      │                                 │
    └───────────────────────────────┘                                 │
    ┌─ Subagent 4 (opt-in) ─────────┐                                 │
    │ social_pulse.gather_…()       │                                 │
    │   tiktok_scroll.scrape()      │                                 │
    │   twitter_scroll.scrape()     │  ←─ Browser-Use Cloud           │
    │   reddit_scroll.scrape()      │     30–90s per platform         │
    │   linkedin_scroll.scrape()    │                                 │
    └───────────────────────────────┘                                 │
    return_exceptions=True   ────────────────────────────────────┐    │
  )                                                              │    │
        │                                                        │    │
        ▼                                                        ▼    ▼
  agent.chat_completion (markdown out)
  generate_campaign(brief, intel, hooks, context, pulse?)
        │
        ▼
  persist_campaign()
    write data/campaigns/<ts>-<slug>.md
    fire-and-forget: nia local sync
        │
        ▼
  scripts.propose_scripts_for_campaign()
    LLM call: build N short-form scripts grounded in pulse + hooks + intel
    OPTIONAL: PATCH /v1/blocks/{NOTION_SCRIPTS_PAGE_ID}/children
        │
        ▼
  automations.propose_automations_for_campaign()
    flatten + dedupe creators from intel
    extract first hook from campaign_md
    build Reacher /automations/dm body (mode/messages/schedule/…)
    AUTOMATIONS_ENABLED + AUTOMATIONS_DRY_RUN gate the actual POST
        │
        ▼
  response JSON: { campaign_markdown, memory_note, subagents, automations, … }
```

Three (or four, with social pulse) independent IO streams plus a sync disk
read run in parallel via `asyncio.gather(…, return_exceptions=True)` — one
bad upstream doesn't sink the response, it just shows up as
`{"error": "…"}` in its slot.

---

## 2. One-screen mental model — insights path

```
              POST /api/social-insights { topic, platforms?, top_n?,
                                          summarize?, scrolls? }
                              │
                              ▼
              social_pulse.social_insights(topic)
                              │
        ┌─────────────────────┴─────────────────────┐
        ▼                                           ▼
  social_pulse.gather_social_pulse()       agent.is_configured() check
  (parallel scrapers, same as subagent 4)
        │                                           │
        ▼                                           ▼
              agent.chat_completion (markdown out)
              summarize_insights(topic, pulse)
                              │
                              ▼
              { topic, platforms, insights_markdown, pulse }
```

No persistence, no Nia sync, no automations. Pure read → summarize. Use
this when you just want a "what's happening on Reddit/X/LinkedIn around
X right now" report.

---

## 3. Why "agentic" instead of one big prompt

A single mega-prompt that asks GPT to "research competitors, find trends,
read brand context, scrape socials, then write a campaign" would do all of
that sequentially inside one call, with no real-world data. The
orchestrator explicitly:

- **Decomposes** the request into a planning step (extract product query)
  and three or four parallel data-gathering steps.
- **Grounds** the synthesis call by feeding it actual API responses + scraped
  posts as a JSON payload, not just the brief.
- **Persists** a compressed summary that becomes input to the next run —
  consecutive campaigns stay coherent without anyone re-uploading brand
  documents.
- **Acts** at the end: the campaign isn't just text, it's the seed for
  outreach automations.

LLM calls happen at exactly two points in the campaign path:

1. **Planner** (`extract_product_query`) — JSON-mode, `temperature=0.0`,
   max_tokens=120. Cheap. Just turns "Make a campaign for a lip gloss
   product" into `{"query": "tinted lip gloss"}`.
2. **Synthesizer** (`generate_campaign`) — markdown out, `temperature=0.7`,
   max_tokens=2000. Receives the full payload of `{brief, competitor_intel,
   trending_hooks, social_pulse?, company_context}` and produces the
   structured campaign markdown.

The insights path uses one LLM call (`summarize_insights`,
`temperature=0.5`, max_tokens=900). Everything else is deterministic
Python + REST/Browser-Use calls.

---

## 4. The four subagents

| # | Subagent | File / function | Sources | Failure mode |
|---|---|---|---|---|
| 1 | Competitor intel | `campaign.gather_competitor_intel` (or `..._for_product_id` if `product_id` is supplied) | Reacher `GET /social-intelligence/products`, then per-product `/.../creators` and `/.../videos` | Retries with a broader query (`_broaden`) if 0 hits. Errors → `{"error": "…"}`. |
| 2 | Trending hooks | `campaign.gather_trending_hooks` | Reacher `GET /social-intelligence/videos/trending` | Same broaden-and-retry pattern. Keeps only hook-relevant fields. |
| 3 | Company context | `campaign.gather_company_context` (sync) | Local disk reads of `data/*.md` listed in `COMPANY_CONTEXT_FILES`, plus `data/campaigns/*.md` (only the `## Memory note` paragraph is extracted via regex). | Missing files silently skipped; no failure. |
| 4 | Social pulse (opt-in) | `social_pulse.gather_social_pulse` | Four Browser-Use Cloud scrapers — `tiktok_scroll.scrape`, `twitter_scroll.scrape`, `reddit_scroll.scrape`, `linkedin_scroll.scrape`. Each spins a cloud session, runs the scroll task, parses JSON output. | Per-platform errors caught and surfaced as `{"error": "…"}` in `results[platform]`. One bad scraper doesn't sink the rest. |

Subagents 1, 2, and 4 are async I/O and run concurrently. Subagent 3 is
fast disk reads, invoked synchronously in the same event loop tick that
schedules the gather.

### Per-request overrides

| Field | Default | What it does |
|---|---|---|
| `product_id` | — | Skip subagent 1's catalog search; pull creators+videos for this exact product. |
| `shop_id` | env `REACHER_SHOP_ID` | Pick which TikTok Shop the Reacher subagents query (and which the DM automation posts under). |
| `include_social_pulse` | `false` | Add subagent 4. |
| `social_platforms` | `["twitter", "reddit", "linkedin"]` | Subset for subagent 4. Accepts `"tiktok"`, `"twitter"` (or `"x"`), `"reddit"`, `"linkedin"`. |

---

## 5. Subagent 4 deep-dive — Browser-Use Cloud scrapers

Each `*_scroll.py` is both a CLI and a library. The library entry is
`async scrape(query, *, scrolls, top_n, profile_id, profile_name,
no_profile, llm) -> {platform, query, items, raw, success}`.

### Profile resolution
Each platform reads its profile id from a per-platform env var:

| Platform | Env var | Falls back to |
|---|---|---|
| TikTok | `BROWSER_USE_TIKTOK_PROFILE_ID` | shared `social` profile |
| Twitter / X | `BROWSER_USE_TWITTER_PROFILE_ID` | shared `social` profile |
| Reddit | `BROWSER_USE_REDDIT_PROFILE_ID` | shared `social` profile |
| LinkedIn | `BROWSER_USE_LINKEDIN_PROFILE_ID` | shared `social` profile |

The shared `social` profile is created (or reused) by `social_login.py` —
one cloud session you log into all four sites with, cookies persist into
the same profile.

### What each scraper does

| Platform | Start URL | Task summary |
|---|---|---|
| TikTok | `/foryou` or `/search/video?q=…` | Scroll the player, return N most-recent video objects. |
| Twitter | `/search?q=…&f=live` (Latest tab) | Scroll the timeline, skip promoted+pinned, return N tweets. |
| Reddit | `/search/?q=…&sort=new&t=day` (with `&t=day` removed as fallback) | Scroll the listing, return N posts. |
| LinkedIn | `/search/results/content/?keywords=…&sortBy="date_posted"` | Scroll the Posts tab, return N posts. |

All four prompts demand a strict JSON array as the agent's output.
`browser_use_common.parse_json_array()` extracts it (handles clean JSON,
JSON-encoded strings, ```json fences, prose wrappers, and over-escaped
quotes). When parsing fails, the response carries `raw_preview` so you can
see what the agent actually returned.

### Output shape from `gather_social_pulse`

```json
{
  "topic": "openai",
  "platforms": ["twitter", "reddit", "linkedin"],
  "items_total": 9,
  "results": {
    "twitter": {
      "platform": "twitter",
      "query": "openai",
      "items": [{"text": "...", "author": "...", "handle": "@...", "posted": "53s",
                 "url": "https://x.com/.../status/...", "summary": "..."}],
      "raw": "<full agent output>",
      "success": true
    },
    "reddit":  { ... same shape ... },
    "linkedin": { ... }
  }
}
```

### Live cloud-session publishing

If `CONVEX_URL` is set, `_run_task` publishes the live cloud session to
Convex via `convex_client.start_cloud_session(platform, query, live_url,
cloud_session_id)`. The dashboard renders the live preview iframe per
platform slot. On task completion `_finish_cloud_session(status, error?)`
updates the row. Convex publishing is best-effort — failures are logged,
never raised.

---

## 6. The synthesizer — `generate_campaign`

System prompt: `CAMPAIGN_SYSTEM_PROMPT` in `campaign.py`. User payload:

```json
{
  "brief": "...",
  "competitor_intel": { ... } | {"error": "..."},
  "trending_hooks":   { ... } | {"error": "..."},
  "social_pulse":     { ... } | {"error": "..."} | null,   // present iff include_social_pulse
  "company_context": {
    "company_docs":         { "company-overview.md": "...", "brand-guide.md": "...", ... },
    "past_campaign_memory": [{ "campaign": "<filename>", "memory_note": "..." }]
  }
}
```

Output: a markdown campaign with this fixed structure (caps from the prompt):

| Section | Cap |
|---|---|
| `# Campaign: …` | distinctive name |
| `## One-line concept` | single sentence |
| `## Hooks` | 3-5 lines, ≤15 words each |
| `## Creator shortlist` | 3-7 bullets — must cite real handles when present in subagent data, otherwise archetypes only (no inventing handles) |
| `## Content plan` | 3-5 video ideas |
| `## Risks / brand checks` | 1-3 bullets — must surface empty subagents |
| `## Memory note` | one paragraph, ≤120 words |

The synthesizer's "HARD CONSTRAINTS" trailer enforces:
* Brand-guide rules (no banned words like "elevate"/"unlock"/"fuel"/etc.)
* No inventing handles or follower counts
* Acknowledge `{"error": …}` or empty subagent slots in Risks

---

## 7. The insights summarizer — `summarize_insights`

Used by `POST /api/social-insights` (and by the Tensorlake-cron tick path
in `main.social_pulse_tick`). System prompt: `INSIGHTS_SYSTEM_PROMPT` in
`social_pulse.py`. Output:

```
# {topic} — pulse

## Summary           (2-3 sentences)
## Key insights      (3-5 bullets, each citing a source post)
## Notable posts     (2-4 bullets with permalinks)
## Gaps              (platforms with no signal)
```

The summarizer is told **not to fabricate handles or stats**, and to put
empty-platform notices in `## Gaps`. Set `summarize=false` on the request
to skip the LLM and just return the structured pulse.

---

## 8. Persistence and the consistency loop (campaign path)

State lives in two places, intentionally separate:

```
data/
├── company-overview.md          ┐
├── brand-guide.md               │  COMPANY_CONTEXT_FILES — full read every run.
├── product-roadmap.md           ┘  Stable. Edit by hand to change the brand.
└── campaigns/
    ├── 20260509-193706-…md      ┐
    ├── 20260509-193739-…md      │  past_campaign_memory — only the
    └── 20260509-193909-…md      ┘  `## Memory note` paragraph is read,
                                    last 8 by mtime.
```

The synthesizer prompt forces the model to end every campaign with a
`## Memory note` paragraph (≤120 words) describing the *distinctive
choices* of THIS campaign. `_extract_memory_note` later regexes that
paragraph out for the next run. That's the entire memory mechanism — no
embeddings, no vector store, just a regex on a section heading.

Two-direction sync to Nia:

1. After `persist_campaign` writes a new file, we fire `nia local sync`
   in a background subprocess (fire-and-forget). Once Nia ingests it,
   the campaign is also queryable via `python pipeline.py chat "…"`.
2. The `data/` folder is registered as a Nia *local-folder source*
   (managed by `pipeline.py`). The orchestrator does NOT query Nia
   during a run — disk reads are faster and don't have semantic-cache
   surprises. Nia is the *out-of-band* index for human chat.

### Resetting memory

```powershell
Remove-Item data\campaigns\*.md
nia local sync   # tell Nia the files are gone
```

The brand docs in `COMPANY_CONTEXT_FILES` are untouched.

---

## 9. The script step — creator scripts + Notion publish

After the campaign markdown is persisted, `scripts.propose_scripts_for_campaign`
produces N short-form video scripts ready to hand to creators and (optionally)
appends them to a Notion page.

### LLM call
System prompt: `SCRIPTS_SYSTEM_PROMPT` in `scripts.py`. JSON-mode, returns:

```json
{
  "scripts": [
    {
      "title": "Glide Test in 15 seconds",
      "creator": "@charlettw",
      "platform": "TikTok",
      "duration_seconds": 15,
      "hook": "Three lip oils. One glides like silk.",
      "beats": [
        {"seconds": 2, "visual": "POV camera on lips, three swatches",
         "voiceover": "First test: drag resistance"},
        {"seconds": 5, "visual": "Slow-mo brush stroke applying oil",
         "voiceover": "Look at that glide — no tug"}
      ],
      "outro": "Sample link in bio. Tag us when you try.",
      "sourced_from": ["twitter:@menter_latonia", "trending:lip-oil-glide"]
    }
  ]
}
```

### Hard constraints (from the prompt)
* `creator` handle MUST appear in `competitor_intel.creators` or
  `social_pulse.results[*].items` — otherwise the script uses `"TBD"`.
* At least 2 phrases per script must be quoted from `social_pulse` /
  `trending_hooks`, with citations in `sourced_from`.
* Brand-guide bans propagate (no "elevate"/"unlock"/etc.).
* No reused hooks across scripts in one batch.

### Notion publishing
PATCH `/v1/blocks/{page_id}/children` with formatted blocks:
* `heading_1` — `{brand} | {campaign_name}`, plus a UTC timestamp
* per script:
  * `heading_2` — `{n}. {title}`
  * `paragraph` — bold-labeled metadata (Creator / Platform / Duration)
  * `heading_3` "Hook" + paragraph
  * `heading_3` "Beats" + numbered list of `[Ns] visual | VO: voiceover`
  * `heading_3` "Outro" + paragraph
  * italic `Sourced from: …` paragraph
  * divider

Headers used:
| Header | Value |
|---|---|
| `Authorization` | `Bearer ${NOTION_API_KEY}` |
| `Notion-Version` | `2022-06-28` (pinned in `scripts.NOTION_VERSION`) |
| `Content-Type` | `application/json` |

Notion limit: 100 children per PATCH. `publish_to_notion` chunks
automatically. Page ID is canonicalized to dashed UUID via
`_format_page_id` (accepts both formats).

### One-time Notion setup
1. Create an integration at https://www.notion.so/my-integrations
2. Copy its **Internal Integration Token** to `NOTION_API_KEY`.
3. Open the target Scripts page in Notion → `…` → **Add connections** →
   select the integration. Without this, every PATCH 404s.
4. Set `NOTION_SCRIPTS_PAGE_ID` to the 32-hex page-ID suffix from the URL.

### Per-call knobs (CampaignRequest)
| Field | Default | Effect |
|---|---|---|
| `publish_scripts` | `false` | When true, attempts the Notion PATCH. When false, scripts are still generated and returned in the response, but NOT pushed. |
| `scripts_count` | `3` | How many scripts to produce. |
| `scripts_page_id` | env `NOTION_SCRIPTS_PAGE_ID` | Per-request override. |
| `brand_name` | `"Aroma Cloud"` | Used in DM template AND Notion heading. |

### Response shape
```json
"scripts": {
  "scripts": [ {...3 script objects...} ],
  "notion": {
    "appended_block_count": 30,
    "page_id": "35b2e5a1-e6d4-80c0-b3df-fd9f5c67bfd8",
    "page_url": "https://www.notion.so/35b2e5a1e6d480c0b3dffd9f5c67bfd8"
  } |  { "skipped": true, "reason": "publish_disabled" | "missing NOTION_API_KEY..." }
      |  { "skipped": false, "error": "<Notion error body>" },
  "published": true | false
}
```

---

## 10. The action step — Reacher DM automation

Once the campaign markdown exists, `automations.propose_automations_for_campaign`
runs unconditionally. What it actually does is governed by env + a per-call
flag:

| Knob | Default | Effect |
|---|---|---|
| `AUTOMATIONS_ENABLED` (env) | `false` | `true` lets the gate evaluate dry-run / live |
| `AUTOMATIONS_DRY_RUN` (env) | `true` | When `ENABLED=true`, `false` means real POST to Reacher |
| `reacher_dry_run` (per-call kwarg) | `false` | Adds `X-Dry-Run: true` header — Reacher validates server-side without persisting. Independent of the env gates. |

Decision matrix:

```
ENABLED=false                          → status: "skipped_disabled"  (payload returned, no call)
ENABLED=true,  DRY_RUN=true            → status: "dry_run"           (payload logged, no call)
ENABLED=true,  DRY_RUN=false,          → status: "submitted"         (Reacher POST happened)
                reacher_dry_run=false
ENABLED=true,  DRY_RUN=false,          → status: "submitted" + Reacher validates without persist
                reacher_dry_run=true
```

`AUTOMATIONS_*` are re-read on every request — flipping `.env` doesn't
require a server restart.

### Reacher request shape (POST `/automations/dm`)

`automations.build_dm_payload` emits:

```json
{
  "automation_name": "Campaign DM 20260509-220708 - Aroma-Cloud",
  "mode": "vanilla",                    // vanilla | with_image | with_product_card | spark_code
  "messages": [{
    "type": "text",
    "body": "Hi {creator_name} — we're Aroma Cloud, ... <hook> ..."
  }],
  "schedule": {
    "daily_cap": 25,
    "timezone": "America/Los_Angeles"
  },
  "creators_to_include": {
    "list_upload": [{"creator_id": "7495440…", "handle": "charlettw"}, ...]
  },
  "_meta": {                            // local only — not part of Reacher's spec
    "brand": "Aroma Cloud",
    "hook_used": "...",
    "target_count": 2,
    "local_render_preview": [{"creator_id": "...", "handle": "...", "message": "..."}]
  }
}
```

`{creator_name}` is left as a placeholder — Reacher's runtime templating
substitutes per creator at delivery time. The `_meta.local_render_preview`
holds our pre-substituted version for inspection only.

### Headers on the create request

| Header | Source | Purpose |
|---|---|---|
| `Idempotency-Key` | UUID per call (overridable via `idempotency_key` kwarg) | Retries don't double-post |
| `X-Created-Via` | `nozomio-orchestrator` constant | Surfaces in Reacher's audit log |
| `X-Dry-Run: true` | Set when `reacher_dry_run=True` | Server-side validate without persist |

### How DM targets are derived

```
result["subagents"]["competitor_intel"]["competitors"][*]["creators"][*]
                              │
                              ▼
        _extract_creator_targets()  — flatten + dedupe on creatorId/handle
                              │
                              ▼
        capped at max_creators (default 50)
                              │
                              ▼
   build_dm_payload() builds the Reacher /automations/dm body
                              │
                              ▼
   create_dm_automation()  — gated POST with Idempotency-Key + X-Dry-Run
```

`CAMPAIGN_CREATORS_PER_PRODUCT` (default 30, max 100) controls how deep
subagent 1 goes per product, which directly determines how many unique
targets the DM step has to work with. With three competitor products at
30 each you typically get 60–90 raw, dropping to ~50 unique after dedupe.

### Iterating the schema with `test_dm_schema.py`

`backend/test_dm_schema.py` is a one-shot probe that:
1. Builds a payload from a fake intel + campaign-md fixture.
2. Forces `AUTOMATIONS_ENABLED=true` + `AUTOMATIONS_DRY_RUN=false` for the
   probe process only.
3. Calls `create_dm_automation(..., reacher_dry_run=True)` so Reacher
   validates without persisting.
4. Prints the response (or 422 details).

Use it to lock down the inner schemas (MessageAddon discriminator,
AutomationSchedule fields, list_upload item shape, templating syntax) once
you have a `read_write`-scoped Reacher API key.

### Future automation types

`automations.py` already has REST path constants for every type Reacher
supports. To add a new one (e.g. sample-request after positive DM
replies), add a `build_sample_request_payload()` helper and a
`create_sample_request_automation()` wrapper that calls `_gated_post()`
with `AUTOMATION_SAMPLE_REQUEST_CREATE_PATH`. The gate handles
ENABLED / DRY_RUN / X-Dry-Run automatically.

---

## 11. Failure semantics

Nothing in the pipeline raises into the response when an upstream is
flaky — every IO branch is wrapped:

| Failure | What appears in the response |
|---|---|
| Subagent 1 raises | `subagents.competitor_intel = {"error": "..."}` and the synthesizer is told about it |
| Subagent 2 raises | `subagents.trending_hooks = {"error": "..."}`, same |
| Subagent 4 (any single platform) raises | `subagents.social_pulse.results.<platform> = {"error": "...", "items": [], "success": false}` |
| All Reacher subagents empty | Synthesizer is prompted to acknowledge in Risks. Hard constraint in the system prompt. |
| `nia local sync` not on PATH | Logged warning, no failure |
| OpenAI raises | Endpoint 500s with the error body — campaign cannot be generated without the synthesizer |
| Automations step raises | `automations = {"error": "..."}`, campaign_markdown still returned |
| Reacher write returns 403 (read-only key) | `automations.dm` carries the 403 body; the campaign is unaffected |

The synthesizer's system prompt also has a "HARD CONSTRAINTS" block:
no inventing handles or follower counts when the data is empty, no
banned brand-guide phrases, and the model must surface empty subagents
in the Risks section.

---

## 12. Environment knobs

```ini
# --- LLM ---
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini

# --- Reacher (TikTok shop intelligence + DM automation) ---
REACHER_API_KEY=...
REACHER_SHOP_ID=1183
CAMPAIGN_CREATORS_PER_PRODUCT=30
AUTOMATIONS_ENABLED=false        # master switch for /automations/* writes
AUTOMATIONS_DRY_RUN=true         # secondary safety; flip both to actually POST

# --- Browser-Use (live social scraping) ---
BROWSER_USE_API_KEY=bu_...
BROWSER_USE_TIKTOK_PROFILE_ID=
BROWSER_USE_TWITTER_PROFILE_ID=
BROWSER_USE_REDDIT_PROFILE_ID=
BROWSER_USE_LINKEDIN_PROFILE_ID=

# --- Notion (creator-script publisher; optional) ---
# Setup: create an integration at https://www.notion.so/my-integrations,
# then in Notion open the Scripts page -> "..." -> Add connections.
NOTION_API_KEY=
NOTION_SCRIPTS_PAGE_ID=35b2e5a1e6d480c0b3dffd9f5c67bfd8

# --- Convex (live-session dashboard publishing; optional) ---
CONVEX_URL=
```

---

## 13. Extending the pipeline

Common extensions and where they go:

| Want to … | Touch this |
|---|---|
| Add a new persistent brand doc | Drop the `.md` in `data/`, add filename to `COMPANY_CONTEXT_FILES` in `campaign.py` |
| Add a 5th subagent | New `gather_X()` async fn in `campaign.py`; add to `asyncio.gather()` in `run_campaign_pipeline`; add to the synthesizer's user payload |
| Add a 5th platform to social pulse | Drop a new `<platform>_scroll.py` next to the existing four (use `browser_use_common.run_scrape_collect`); register it in `social_pulse.PLATFORMS` |
| Use a different LLM | `agent.chat_completion()` reads `OPENAI_MODEL`. Swap the SDK in `agent.py` if changing providers |
| Tighten the synthesis prompt | `CAMPAIGN_SYSTEM_PROMPT` in `campaign.py` — has a "HARD CONSTRAINTS" trailer for rules the model must obey silently |
| Make campaigns longer / richer | Loosen the per-section caps in `CAMPAIGN_SYSTEM_PROMPT` (currently aggressive: 3-5 hooks @ 15w, etc.) and bump `max_tokens` in `generate_campaign` |
| Change the memory format | Edit `_extract_memory_note` regex (currently matches `## Memory note`) and the `## Memory note` template inside `CAMPAIGN_SYSTEM_PROMPT` |
| Add per-creator DM personalization | Extend `automations.build_dm_payload` — Reacher supports `{creator_name}` runtime substitution today, plus a per-creator override field once we confirm its name with the dry-run probe |
| Add a new Reacher automation type | New `build_*_payload` + `create_*_automation` in `automations.py`, both routed through `_gated_post` so they inherit the `AUTOMATIONS_*` gates and Idempotency / X-Dry-Run header support |
| Change script tone / structure | `SCRIPTS_SYSTEM_PROMPT` in `scripts.py`. The prompt enforces JSON-mode + per-script hard rules (real handles only, ≥2 cited phrases, beats fit duration). |
| Publish scripts somewhere other than Notion | Replace `publish_to_notion()` in `scripts.py` with another sink (Linear, Slack, Google Doc). The block-formatting helpers (`_heading`, `_paragraph`, `_numbered`) are Notion-specific; everything before that is target-agnostic. |

---

## 14. End-to-end test

`backend/test_e2e.py` exercises the full campaign loop without going
through HTTP. From `backend/`:

```powershell
uv run python test_e2e.py
```

It snapshots context → runs two campaigns → asserts that run 2's context
contains run 1's memory note → confirms `nia local status` is healthy.
Costs roughly $0.02 per run in OpenAI tokens plus a handful of Reacher
requests.

`backend/test_dm_schema.py` (separate) probes the Reacher
`/automations/dm` create endpoint with `X-Dry-Run: true`. Returns 403
under a read-only key; the moment the key is upgraded to `read_write`
scope, run it again and iterate against Reacher's validator.

For the HTTP path:
* `POST /api/marketing-campaign` — full campaign + (optional) social pulse
* `POST /api/social-insights` — pure cross-platform listening, no campaign
* `POST /api/social-pulse/tick` — Tensorlake-cron-fired pulse summary that
  also DMs the markdown to a recipient (see
  [`how-to-run-tensorlake-photon.md`](./how-to-run-tensorlake-photon.md))
