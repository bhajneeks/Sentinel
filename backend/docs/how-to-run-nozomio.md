# How to run Nozomio

End-to-end instructions to bring up the Nozomio backend (FastAPI + Reacher
TikTok Shop integration + Nia document pipeline) on a fresh machine.

---

## 1. Prerequisites

| Tool | Why | Install |
|------|-----|---------|
| Python 3.13+ | Backend runtime (`pyproject.toml` pins `>=3.13`) | https://www.python.org/downloads/ |
| `uv` | Project + venv manager used by the backend | https://docs.astral.sh/uv/getting-started/installation/ |
| Node 20+ and npm | Frontend (Next.js 16) and the global `nia` CLI | https://nodejs.org/ |
| `nia` CLI | Document ingestion + chat | `npm install -g @nozomioai/nia@latest` |

Verify everything resolves:

```powershell
python --version    # 3.13.x
uv --version
node --version
nia auth status     # should report "Authenticated"
```

If `nia auth status` says you're not logged in, run:

```powershell
nia auth login --api-key <your nk_... key>
```

---

## 2. Get the code

```powershell
git clone <repo-url> nozomioABC
cd nozomioABC
```

Layout you should see:

```
nozomioABC/
├── backend/        # FastAPI app — this guide
│   ├── docs/
│   ├── main.py
│   ├── pipeline.py        # Nia ingestion + chat CLI
│   ├── pipeline.json      # source manifest (gitignored)
│   ├── reacher.py         # Reacher TikTok Shop API client
│   ├── pyproject.toml
│   └── uv.lock
├── frontend/       # Next.js 16 app
├── data/           # drop documents here for the Nia pipeline
└── README.md
```

---

## 3. Backend secrets — `backend/.env`

The backend reads secrets from `backend/.env` (loaded by `python-dotenv` in
`main.py`). The file is gitignored — never commit it.

Create `backend/.env` with:

```dotenv
# Reacher (TikTok Shop social intelligence)
REACHER_API_KEY=your-reacher-api-key
REACHER_SHOP_ID=all          # or a specific shop id

# Optional: pin a Nia API key to this project so `pipeline.py` doesn't
# rely on the globally authenticated CLI. If unset, the pipeline uses
# whatever `nia auth login` configured globally.
# NIA_API_KEY=nk_...
```

The two Reacher variables are **required** if you call `/api/trending-videos`
or `/api/competitors`. The hello endpoint works without them.

---

## 4. Install backend dependencies

```powershell
cd backend
uv sync
```

`uv sync` reads `pyproject.toml` + `uv.lock` and creates `.venv/` with the
exact pinned versions. No need to activate the venv manually — `uv run`
handles that.

---

## 5. Start the backend

```powershell
# from backend/
uv run uvicorn main:app --reload --port 8000
```

Smoke-test it from another terminal:

```powershell
curl http://localhost:8000/api/hello
# {"message": "f from FastAPI"}
```

Once secrets are in place:

```powershell
curl "http://localhost:8000/api/trending-videos?interest=skincare&page_size=5"
curl "http://localhost:8000/api/competitors?query=glossy+pink+lip+oil&top_products=3"
```

OpenAPI docs are auto-generated at <http://localhost:8000/docs>.

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/hello` | Liveness probe |
| `GET` | `/api/trending-videos` | Top-performing TikTok Shop videos for an interest |
| `GET` | `/api/competitors` | Competitor product landscape (products → creators + videos) |
| `POST` | `/api/marketing-campaign` | Multi-subagent campaign orchestrator (see §7) |

Query-param contracts live in `backend/main.py`; allowed values for
`sort_by` / `time_range` come from `backend/reacher.py`.

---

## 6. Start the frontend (optional)

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open <http://localhost:3000>. The frontend hits the FastAPI app over
CORS — `main.py` whitelists `http://localhost:3000` only.

---

## 7. Marketing-campaign orchestrator

`POST /api/marketing-campaign` ties Reacher and Nozomio (Nia) together
behind a single LLM-driven entry point. It's implemented in
[`backend/campaign.py`](../campaign.py) and uses [`backend/agent.py`](../agent.py)
for all OpenAI calls.

### How it works

```
brief (free text)
  │
  ▼  agent.chat_completion (json mode)
extract_product_query(brief)         e.g. "Make a campaign for a lip gloss
                                      product"  ->  query="tinted lip gloss"
  │
  ▼  asyncio.gather(...)  — three subagents run in parallel
  │
  ├── 1. gather_competitor_intel(query)
  │      → reacher.get_competitor_landscape (products → creators + videos)
  │
  ├── 2. gather_trending_hooks(query)
  │      → reacher.get_trending_videos, then keep only hook-relevant
  │        metadata (title, caption, content_tags, ai_tags, engagement)
  │
  └── 3. gather_company_context()
         → reads every file in COMPANY_CONTEXT_FILES from data/, currently:
             - company-overview.md   (org / numbers / leadership)
             - brand-guide.md        (voice, audience, channel + creator rules)
             - product-roadmap.md    (what's actually shipping)
         → reads data/campaigns/*.md (compressed memory notes from past runs)
  │
  ▼  agent.chat_completion (markdown out)
generate_campaign(brief, intel, hooks, context)
  │
  ▼
persist_campaign(...)
  → writes data/campaigns/<timestamp>-<slug>.md
  → fires `nia local sync` (background, fire-and-forget)
```

The Nia local-folder source already covers `data/`, so once `nia local sync`
finishes, the new campaign and its `## Memory note` paragraph are queryable
via `pipeline.py chat` *and* picked up by the next campaign run as context.

### Request / response

```bash
curl -X POST http://localhost:8000/api/marketing-campaign \
  -H "Content-Type: application/json" \
  -d '{"brief":"Make a marketing campaign for a lip gloss product"}'
```

```jsonc
{
  "brief": "Make a marketing campaign for a lip gloss product",
  "extracted_query": "tinted lip gloss",
  "campaign_markdown": "# Campaign: ...\n\n## One-line concept\n...",
  "memory_note": "Compressed paragraph describing the distinctive choices.",
  "saved_to": "C:\\...\\data\\campaigns\\20260509-181203-tinted-lip-gloss.md",
  "subagents": {
    "competitor_intel": { "query": "...", "competitor_count": 3, "competitors": [ ... ] },
    "trending_hooks":   { "query": "...", "hooks": [ ... ] },
    "company_context":  {
      "loaded_files": ["company-overview.md", "brand-guide.md", "product-roadmap.md"],
      "past_campaign_count": 4
    }
  }
}
```

If a Reacher subagent fails (e.g. invalid `REACHER_SHOP_ID`), its slot is
returned as `{"error": "..."}` and the LLM is still given the other two
streams plus the failure context — it will produce a campaign with explicit
caveats rather than silently 500.

### Required env

- `OPENAI_API_KEY` — used by `agent.chat_completion`. Optional `OPENAI_MODEL`
  (defaults to `gpt-4o-mini`).
- `REACHER_API_KEY` + `REACHER_SHOP_ID` — see §3. Use a single shop_id; the
  per-product creators/videos endpoints don't accept `all`.

### Outreach automations (post-campaign step)

After the campaign markdown is generated and saved, the orchestrator
ALWAYS calls `automations.propose_automations_for_campaign()`. The result
appears under `result["automations"]` in the response.

What it does today:

- **DM Outreach** — flattens every creator from `competitor_intel`,
  dedupes on `creatorId`/`handle`, takes the first hook from the
  campaign markdown, renders a per-creator message from
  `automations.DEFAULT_DM_TEMPLATE`, and builds the JSON body for
  `POST /automations/dm`.

Whether anything actually hits Reacher is gated by **two** env vars
(both default to safe / no-op):

| Env var | Default | Effect |
|---|---|---|
| `AUTOMATIONS_ENABLED` | `false` | If false, the planned payload is returned with `status: "skipped_disabled"` — no Reacher call. |
| `AUTOMATIONS_DRY_RUN` | `true`  | Even with the master switch on, the POST is skipped and the payload is logged. Returned with `status: "dry_run"`. Flip to `false` to actually fire. |
| `CAMPAIGN_CREATORS_PER_PRODUCT` | `30` | How many creators per competitor product to pull. Reacher caps at 100. |

Recommended rollout:

1. Default config — verify the planned payload looks right
   (`status: "skipped_disabled"`).
2. Set `AUTOMATIONS_ENABLED=true` (still dry-run) — confirm logs show
   the exact payload and target count.
3. Set `AUTOMATIONS_DRY_RUN=false` — automations will create on Reacher.

Future automation types proposed in the playbook (helpers exist as REST
paths in `automations.py`, build them out as needed):

- `target-collab` — invite specific creators to a Collab.
- `tc-cleanup` — sweep stale Target Collab invites after N days.
- `email` — same as DM but over email channel.
- `sample-request` — auto-process sample shipments for creators who reply
  positively to the DM.

### Memory / consistency loop

Persistent context comes from two sources:

1. **Brand docs** — `data/company-overview.md`, `data/brand-guide.md`, and
   `data/product-roadmap.md`. The list lives in `COMPANY_CONTEXT_FILES` at
   the top of `campaign.py` — to add a new persistent doc (e.g.
   `pricing-rules.md`), drop it in `data/` and append the filename there.
   These files are loaded in full on every run, so they always override
   anything the LLM might drift toward.
2. **Past-campaign memory** — `data/campaigns/*.md`. The orchestrator pulls
   just the `## Memory note` paragraph (via regex) from the eight most-recent
   files. Short enough to fit in context, dense enough to keep brand voice
   and creator angles consistent run-to-run.

To reset the memory: delete files under `data/campaigns/` and re-run
`nia local sync` so Nia drops them from its index. The brand docs in (1) are
not touched.

---

## 8. Nia document pipeline

`backend/pipeline.py` is a small CLI that ingests text/markdown/CSV/PDF/Excel
into Nia and lets you chat across them. Documents go in the `data/` folder
at the repo root.

### One-time setup

You only need this once per machine (already done if `nia auth status`
shows authenticated):

```powershell
nia auth login --api-key <nk_...>
```

### Ingest

```powershell
# from repo root or backend/
python backend/pipeline.py ingest                    # picks up new files in ../data
python backend/pipeline.py ingest path/to/file.pdf   # single file
python backend/pipeline.py list                      # show what's tracked
```

What goes where:

- **Text-like files** (`.txt`, `.md`, `.csv`, `.tsv`, `.json`, `.yaml`,
  `.log`, `.rst`) — registered as part of one **local-folder** Nia source.
  Edits sync via `nia local sync`.
- **Binary docs** (`.pdf`, `.xls`, `.xlsx`) — uploaded individually via
  `nia sources upload` and tracked in `pipeline.json`.

Re-running `ingest` is idempotent. Files already in the manifest are
skipped; new files are added.

### Chat

```powershell
# Cross-document semantic Q&A over everything you've ingested:
python backend/pipeline.py chat "Which port does the FastAPI backend run on?"

# Single-document agent (citations, deeper reasoning) — uploaded docs only:
python backend/pipeline.py ask my-paper "summarize the methodology"
```

`chat` runs `nia search query` scoped to the local-folder id and any
uploaded-doc ids stored in `pipeline.json`.

`ask` runs `nia document agent` against one PDF/Excel source by name or id.
Use `python backend/pipeline.py list` to see the available names.

### Updating the index after editing files in `data/`

```powershell
nia local sync           # push file changes to Nia
```

The `chat` command answers from the most recent sync. Nia caches answers
semantically — if you re-ask the same question and get a stale answer,
rephrase it.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `ReacherConfigError: REACHER_API_KEY ... must be set` | `.env` missing or not loaded | Confirm `backend/.env` exists and `load_dotenv()` runs (it does, at the top of `main.py`) |
| `nia: command not found` | Global `nia` CLI not installed | `npm install -g @nozomioai/nia@latest` |
| `Authentication required` from pipeline | CLI not logged in | `nia auth login --api-key <nk_...>` |
| `pipeline.py chat` returns "not available in the documentation" but data exists | Semantic cache hit on a previous query | Rephrase the question, or wait ~1 min after a fresh sync before retrying |
| Upload fails with `body → type: Field required` | Trying to upload a file type the API doesn't accept (e.g. CSV) | The script already routes CSV/TSV through the local-folder path — only `.pdf`, `.xls`, `.xlsx` use the upload endpoint |
| Windows `UnicodeEncodeError: 'charmap' codec` | Terminal can't render non-ASCII Nia output | The script reconfigures stdout/stderr to UTF-8 — if you call `nia` directly, run `chcp 65001` first or `$env:PYTHONIOENCODING = "utf-8"` |
| Frontend gets CORS error | Hitting backend from a non-`localhost:3000` origin | Edit `allow_origins` in `main.py` |
| `/api/marketing-campaign` returns `OPENAI_API_KEY is not set` | Missing key in `backend/.env` | Add `OPENAI_API_KEY=sk-...` and restart uvicorn |
| Campaign output ignores past runs | `data/campaigns/` empty, or Nia not synced | Confirm at least one prior `*.md` exists; run `nia local sync` manually |
| Campaign keeps repeating the same angles | Memory notes too generic | Manually edit the `## Memory note` paragraph of recent campaign files to be more specific, then re-run |
| Only ~6 creators per product in the response | Pre-bump default | Set `CAMPAIGN_CREATORS_PER_PRODUCT=30` (or any value 1–100) in `.env.local` |
| `automations.dm.status: "skipped_disabled"` even when I want to fire | Master switch off | Set `AUTOMATIONS_ENABLED=true` (and review the dry-run payload before flipping `AUTOMATIONS_DRY_RUN=false`) |
| `automations.skipped: "no usable creator handles"` | Reacher returned creators without `handle`/`creatorId` (rare) | Re-run with a different `query` or supply a `product_id` directly |

---

## 10. Quick reference — full local startup

Three terminals:

```powershell
# Terminal 1 — backend
cd backend
uv sync
uv run uvicorn main:app --reload --port 8000

# Terminal 2 — frontend
cd frontend
npm install
npm run dev

# Terminal 3 — ingest some docs and chat
python backend/pipeline.py ingest
python backend/pipeline.py chat "what's in the roadmap for Q2 2026?"
```

That's the whole loop.
