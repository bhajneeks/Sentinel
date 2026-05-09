# Agentic pipeline

How the marketing-campaign orchestrator is wired. Companion to
[`how-to-run-nozomio.md`](./how-to-run-nozomio.md) (ops) and
[`how-to-run-reacher.md`](./how-to-run-reacher.md) (single-endpoint
reference). This doc is the *architecture* view: what calls what, where
the LLM enters and leaves, and where state lives between runs.

---

## 1. One-screen mental model

```
                          POST /api/marketing-campaign
                          { brief, product_id?, shop_id? }
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
    │   then keep only hook-rele-   │                                 │
    │   vant fields                 │                                 │
    └───────────────────────────────┘                                 │
    return_exceptions=True   ────────────────────────────────────┐    │
  )                                                              │    │
        │                                                        │    │
        ▼                                                        ▼    ▼
  agent.chat_completion (markdown out)
  generate_campaign(brief, intel, hooks, context)
        │
        ▼
  persist_campaign()
    write data/campaigns/<ts>-<slug>.md
    fire-and-forget: nia local sync
        │
        ▼
  automations.propose_automations_for_campaign()
    flatten + dedupe creators from intel
    extract first hook from campaign_md
    render per-creator DM messages
    AUTOMATIONS_ENABLED + AUTOMATIONS_DRY_RUN gate the actual POST
        │
        ▼
  response JSON: { campaign_markdown, memory_note, subagents, automations, ... }
```

Three independent IO streams (subagent 1, subagent 2, disk read) run in
parallel via `asyncio.gather(..., return_exceptions=True)` — one bad
upstream doesn't sink the response, it just shows up as
`{"error": "..."}` in its slot.

---

## 2. Why "agentic" instead of one big prompt

A single mega-prompt that asks GPT to "research competitors, find trends,
read brand context, then write a campaign" would do all of that
sequentially inside one call, with no real-world data. The orchestrator
explicitly:

- **Decomposes** the request into a planning step (extract product query)
  and three parallel data-gathering steps (subagents 1–3).
- **Grounds** the synthesis call by feeding it actual API responses as a
  JSON payload, not just the brief.
- **Persists** a compressed summary that becomes input to the next run —
  so consecutive campaigns stay coherent without anyone re-uploading
  brand documents.
- **Acts** at the end: the campaign isn't just text, it's the seed for
  outreach automations.

LLM calls happen at exactly two points:

1. **Planner** (`extract_product_query`) — JSON-mode, `temperature=0.0`,
   max_tokens=120. Cheap. Just turns "Make a campaign for a lip gloss
   product" into `{"query": "tinted lip gloss"}`.
2. **Synthesizer** (`generate_campaign`) — markdown out, `temperature=0.7`,
   max_tokens=2000. Receives the full payload of
   `{brief, competitor_intel, trending_hooks, company_context}` and
   produces the structured campaign markdown.

Everything else is deterministic Python + REST calls.

---

## 3. The three subagents

| Subagent | File / function | Reacher endpoint(s) | Failure mode |
|---|---|---|---|
| 1. Competitor intel | `campaign.gather_competitor_intel` (or `..._for_product_id` if `product_id` is supplied) | `GET /social-intelligence/products`, then per-product `/.../creators` and `/.../videos` | Retries with a broader query (`_broaden`) if 0 hits. Errors become `{"error": "..."}` in the slot. |
| 2. Trending hooks | `campaign.gather_trending_hooks` | `GET /social-intelligence/videos/trending` | Same broaden-and-retry pattern. Keeps only hook-relevant fields (`title`, `caption`, `content_tags`, `ai_tags`, `topics`, `engagement`, …). |
| 3. Company context | `campaign.gather_company_context` (sync) | None — local disk reads of `data/*.md` files listed in `COMPANY_CONTEXT_FILES`, plus `data/campaigns/*.md` (only the `## Memory note` paragraph is extracted via regex). | Missing files are silently skipped; no failure. |

The first two are async I/O against Reacher and run concurrently. The
third is fast disk reads, so it's invoked synchronously in the same event
loop tick that schedules the gather.

### Per-request overrides

Both Reacher subagents accept `shop_id` per call (defaults to
`REACHER_SHOP_ID` env). Subagent 1 also accepts `product_id` to skip the
catalog search entirely — useful when the LLM-extracted query is too
niche to match the TikTok Shop catalog (the original "Yirgacheffe"
problem).

---

## 4. Persistence and the consistency loop

State lives in two places, intentionally separate:

```
data/
├── company-overview.md          ┐
├── brand-guide.md               │  COMPANY_CONTEXT_FILES — full read every run.
├── product-roadmap.md           ┘  Stable. Edit by hand to change the brand.
└── campaigns/
    ├── 20260509-193706-...md    ┐
    ├── 20260509-193739-...md    │  past_campaign_memory — only the
    └── 20260509-193909-...md    ┘  `## Memory note` paragraph is read,
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
   the campaign is also queryable via `python pipeline.py chat "..."`.
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

## 5. The action step (automations)

Once the campaign markdown exists, `automations.propose_automations_for_campaign`
runs unconditionally. What it actually does is governed by env:

| Env | Default | If set otherwise |
|---|---|---|
| `AUTOMATIONS_ENABLED` | `false` | `true` lets the gate evaluate dry-run |
| `AUTOMATIONS_DRY_RUN` | `true` | `false` means real POST to Reacher |

Decision matrix:

```
ENABLED=false                 → status: "skipped_disabled"  (payload returned, no call)
ENABLED=true,  DRY_RUN=true   → status: "dry_run"           (payload logged, no call)
ENABLED=true,  DRY_RUN=false  → status: "submitted"         (Reacher POST happened)
```

Both flags are re-read on every request — flipping `.env.local` doesn't
require a server restart.

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
   for each target: render DEFAULT_DM_TEMPLATE with {creator_name, hook, brand}
        hook = first bullet under `## Hooks` in the campaign_md
                              │
                              ▼
   build_dm_payload() returns the JSON body for POST /automations/dm
```

`CAMPAIGN_CREATORS_PER_PRODUCT` (default 30, max 100) controls how deep
subagent 1 goes per product, which directly determines how many unique
targets the DM step has to work with. With three competitor products at
30 each you typically get 60–90 raw, dropping to ~50 unique after dedupe.

### Future automation types

`automations.py` already has REST path constants for every type Reacher
supports. To add a new one (e.g. sample-request after positive DM
replies), add a `build_sample_request_payload()` helper and a
`create_sample_request_automation()` wrapper that calls `_gated_post()`
with `AUTOMATION_SAMPLE_REQUEST_CREATE_PATH`. The gate handles
ENABLED/DRY_RUN automatically.

---

## 6. Failure semantics

Nothing in the pipeline raises into the response when an upstream is
flaky — every IO branch is wrapped:

| Failure | What appears in the response |
|---|---|
| Subagent 1 raises | `subagents.competitor_intel = {"error": "..."}` and the synthesizer is told about it |
| Subagent 2 raises | `subagents.trending_hooks = {"error": "..."}`, same |
| Both empty (Reacher returned nothing) | Synthesizer is prompted to acknowledge in Risks. Hard constraint in the system prompt. |
| `nia local sync` not on PATH | Logged warning, no failure |
| OpenAI raises | The whole endpoint 500s with the error body — campaign cannot be generated without the synthesizer |
| Automations step raises | `automations = {"error": "..."}`, campaign_markdown is still returned |

The synthesizer's system prompt also has a "HARD CONSTRAINTS" block:
no inventing handles or follower counts when the data is empty, no
banned brand-guide phrases, and the model must surface empty subagents
in the Risks section.

---

## 7. Extending the pipeline

Common extensions and where they go:

| Want to … | Touch this |
|---|---|
| Add a new persistent brand doc | Drop the `.md` in `data/`, add filename to `COMPANY_CONTEXT_FILES` in `campaign.py` |
| Add a 4th subagent (e.g. "scrape Twitter for brand mentions") | New `gather_X()` async fn in `campaign.py`; add to `asyncio.gather()` in `run_campaign_pipeline`; add to the synthesizer's user payload |
| Use a different LLM | `agent.chat_completion()` reads `OPENAI_MODEL`. Swap the SDK in `agent.py` if changing providers |
| Tighten the synthesis prompt | `CAMPAIGN_SYSTEM_PROMPT` in `campaign.py` — has a "HARD CONSTRAINTS" trailer for rules the model must obey silently |
| Change the memory format | Edit `_extract_memory_note` regex (currently matches `## Memory note`) and the `## Memory note` template inside `CAMPAIGN_SYSTEM_PROMPT` |
| Add per-creator DM personalization | Replace `DEFAULT_DM_TEMPLATE` and `_first_hook` logic in `automations.py` with an LLM call that takes the creator's last 3 video captions as input |

---

## 8. End-to-end test

`backend/test_e2e.py` exercises the full loop without going through
HTTP. From `backend/`:

```powershell
uv run python test_e2e.py
```

It snapshots context → runs two campaigns → asserts that run 2's context
contains run 1's memory note → confirms `nia local status` is healthy.
Costs roughly $0.02 per run in OpenAI tokens plus a handful of Reacher
requests.

For the HTTP path, hit `POST /api/marketing-campaign` directly — same
orchestrator, same response shape, plus FastAPI's request validation.
