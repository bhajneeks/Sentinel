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

## 7. Nia document pipeline

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

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `ReacherConfigError: REACHER_API_KEY ... must be set` | `.env` missing or not loaded | Confirm `backend/.env` exists and `load_dotenv()` runs (it does, at the top of `main.py`) |
| `nia: command not found` | Global `nia` CLI not installed | `npm install -g @nozomioai/nia@latest` |
| `Authentication required` from pipeline | CLI not logged in | `nia auth login --api-key <nk_...>` |
| `pipeline.py chat` returns "not available in the documentation" but data exists | Semantic cache hit on a previous query | Rephrase the question, or wait ~1 min after a fresh sync before retrying |
| Upload fails with `body → type: Field required` | Trying to upload a file type the API doesn't accept (e.g. CSV) | The script already routes CSV/TSV through the local-folder path — only `.pdf`, `.xls`, `.xlsx` use the upload endpoint |
| Windows `UnicodeEncodeError: 'charmap' codec` | Terminal can't render non-ASCII Nia output | The script reconfigures stdout/stderr to UTF-8 — if you call `nia` directly, run `chcp 65001` first or `$env:PYTHONIOENCODING = "utf-8"` |
| Frontend gets CORS error | Hitting backend from a non-`localhost:3000` origin | Edit `allow_origins` in `main.py` |

---

## 9. Quick reference — full local startup

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
