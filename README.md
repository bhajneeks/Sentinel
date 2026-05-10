# Sentinel

An always-on brand intelligence agent built on Nozomio's platform. Sentinel monitors the social web for brand mentions, orchestrates multi-agent marketing campaigns, and manages creator outreach — all through a conversational AI (Rachel) you talk to over iMessage.

## Architecture

```
 iPhone / iMessage          FastAPI Backend              Next.js Frontend
       │                          │                            │
       ▼                          ▼                            ▼
 ┌──────────────┐  POST    ┌─────────────────┐   SSE   ┌────────────────────┐
 │  bridge/     │─────────▶│  /ingest        │────────▶│  /dashboard        │
 │  (Bun +      │          │  Rachel (OpenAI)│         │  /monitor          │
 │  Photon AI)  │◀─────────│  → iMessage out │         │  Convex real-time  │
 └──────────────┘  reply   └─────────────────┘         └────────────────────┘
                                   │
                    ┌──────────────┼──────────────────┐
                    ▼              ▼                   ▼
             ┌──────────┐  ┌────────────┐   ┌─────────────────────┐
             │ Campaign  │  │  Monitor   │   │  Browser-Use Cloud  │
             │ Pipeline  │  │  Agent     │   │  (X, LinkedIn,      │
             │ (Reacher  │  │ (Tensorlake│   │   Reddit, TikTok)   │
             │  + Nia)   │  │  Sandbox   │   │  → Convex stream    │
             └──────────┘  │  Pools)    │   └─────────────────────┘
                           └────────────┘
                                 │
                      iMessage alert on high-signal mention
```

**Components:**

- `bridge/` — Bun watcher using [`@photon-ai/imessage-kit`](https://github.com/photon-hq/imessage-kit). Tails `~/Library/Messages/chat.db` and POSTs each inbound message to the backend; Rachel's replies are sent back as iMessages.
- `backend/` — FastAPI (managed by [uv](https://docs.astral.sh/uv/)). Hosts Rachel (OpenAI reply agent with tool-calling), the brand monitor, the campaign pipeline, the scraper stream, and all API endpoints.
- `frontend/` — Next.js 16 (App Router, React 19, Tailwind 4). `/dashboard` shows live iMessage threads + a 3D agent-mesh scene. `/monitor` shows the brand monitoring feed, run history, and config panel.

---

## What Sentinel does

### 1. Always-on brand monitoring (Tensorlake Sandbox Pools)

Sentinel runs a background agent on a configurable schedule. Each run:

- Claims a pre-warmed Tensorlake sandbox container in a single call (near-instant, no cold boot)
- Scrapes Reddit, X, and LinkedIn for your brand terms
- Deduplicates against a persistent seen-ID store — you never get alerted twice
- Scores each mention against adaptive per-platform engagement baselines
- Fires an iMessage alert to you when something high-signal breaks through

Configure it at `/monitor` or via API. The agent adapts its signal threshold over time as it accumulates history.

### 2. Multi-agent campaign pipeline

Send Rachel a product brief over iMessage (e.g. *"Make a campaign for a lip gloss"*) and three subagents run in parallel:

1. **Competitor Intel** — Reacher pulls similar TikTok Shop products and the creators driving their sales
2. **Trending Hooks** — Reacher surfaces top-performing video metadata in that category
3. **Brand Context** — Nia loads your brand guide, company overview, and memory notes from past campaigns

These are synthesized into a full campaign brief (concept, hooks, channel strategy, creator list, success metrics), saved to `data/campaigns/`, and synced to Nia so the next run learns from this one.

### 3. Creator DM automation

After a campaign is generated, Sentinel proposes a personalized DM campaign to the extracted creators via Reacher. Two safety gates prevent accidental sends — flip `AUTOMATIONS_ENABLED` and `AUTOMATIONS_DRY_RUN` when you're ready to fire.

Creator scripts can be published directly to **Notion** (`NOTION_API_KEY` + `NOTION_SCRIPTS_PAGE_ID`).

### 4. Supervised browser agents

Rachel can spawn up to 4 supervised browser agents (X, LinkedIn, Reddit, TikTok) via Browser-Use Cloud. They run continuously, stream live mentions into Convex, and appear in the `/dashboard` in real time. Rachel can `redirect`, `screenshot`, `close`, or `spawn` agents mid-session.

### 5. Rachel — conversational AI via iMessage

Rachel (OpenAI GPT) is the interface for everything. Text your number and she can:
- Track a company (`track_company`) — spawns 4 browser agents and monitors it
- Search Reddit, X, or LinkedIn on demand
- Create a marketing campaign from a brief
- Screenshot what a browser agent is currently seeing
- Save notes about companies for future reference

---

## Prerequisites

| Tool | Why | Install |
|------|-----|---------|
| macOS with Messages.app | Bridge reads `chat.db` | — |
| **Full Disk Access** for terminal | Bridge needs to tail `chat.db` | System Settings → Privacy & Security → Full Disk Access |
| Python 3.13+ | Backend runtime | https://www.python.org/downloads/ |
| `uv` | Backend dependency manager | https://docs.astral.sh/uv/getting-started/installation/ |
| Node 20+ / npm | Frontend + Nia CLI | https://nodejs.org/ |
| Bun ≥ 1.0 | Bridge runtime | https://bun.sh |
| `nia` CLI | Document ingestion + campaign context | `npm install -g @nozomioai/nia@latest` |

---

## Setup

```sh
cp backend/.env.example backend/.env.local
# fill in the keys below
```

**Required:**

```dotenv
OPENAI_API_KEY=sk-...              # Rachel (iMessage reply agent)
REACHER_API_KEY=...                # TikTok Shop intel + DM automation
REACHER_SHOP_ID=all
TENSORLAKE_API_KEY=tl_apiKey_...  # Brand monitoring sandbox pools
```

**Optional but recommended:**

```dotenv
BROWSER_USE_API_KEY=...            # Supervised browser agents (X, LinkedIn, TikTok)
CONVEX_URL=https://...             # Real-time DB for scraper stream + dashboard
NOTION_API_KEY=...                 # Publish creator scripts to Notion
NOTION_SCRIPTS_PAGE_ID=...

# Automation safety gates (default: off/dry-run)
AUTOMATIONS_ENABLED=false
AUTOMATIONS_DRY_RUN=true
CAMPAIGN_CREATORS_PER_PRODUCT=30
```

Authenticate Nia (one-time):

```sh
nia auth login --api-key <nk_...>
nia auth status   # should say Authenticated
```

---

## Run

Open three terminals.

**Backend** (port 8000):

```sh
cd backend
uv sync
uv run uvicorn main:app --reload --port 8000
```

**Frontend** (port 3000):

```sh
cd frontend
npm install
npm run dev -- --webpack   # Turbopack not supported on darwin/arm64
```

**Bridge** (iMessage watcher):

```sh
cd bridge
bun install
bun run start
```

Open http://localhost:3000/dashboard for the iMessage interface and http://localhost:3000/monitor for the brand monitoring feed.

---

## API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/hello` | Liveness probe |
| `GET` | `/api/agent/status` | Whether `OPENAI_API_KEY` is configured |
| `GET` | `/api/messages/stream` | SSE stream of all messages |
| `GET` | `/api/conversations` | Active conversation list |
| `POST` | `/api/messages/ingest` | Inbound message from bridge |
| `GET` | `/api/trending-videos` | Top TikTok Shop videos for an interest |
| `GET` | `/api/competitors` | Competitor products + creators |
| `POST` | `/api/marketing-campaign` | Run full campaign pipeline |
| `POST` | `/api/monitor/config` | Set brand terms + start scheduler |
| `GET` | `/api/monitor/status` | Monitor state + next run time |
| `GET` | `/api/monitor/history` | Mention feed + run history |
| `POST` | `/api/monitor/trigger` | Trigger a monitor run immediately |

---

## Nia document pipeline

Drop brand docs into `data/` and ingest them so Rachel and the campaign pipeline can reference them:

```sh
# Ingest everything in data/
python backend/pipeline.py ingest

# Chat across all ingested docs
python backend/pipeline.py chat "what's the brand voice for Gen Z audiences?"

# Sync after editing files
nia local sync
```

Supported formats: `.md`, `.txt`, `.csv`, `.json`, `.pdf`, `.xlsx`. Campaign files are auto-synced after each run so future campaigns learn from past ones.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `nia: command not found` | `npm install -g @nozomioai/nia@latest` |
| `Authentication required` from pipeline | `nia auth login --api-key <nk_...>` |
| Bridge can't read `chat.db` | Grant Full Disk Access to Terminal in System Settings |
| CORS error on frontend | Add your origin to `allow_origins` in `backend/main.py` |
| `automations.dm.status: "skipped_disabled"` | Set `AUTOMATIONS_ENABLED=true` in `.env.local` |
| Monitor not showing in Tensorlake dashboard | Backend must be running and you must click **Save & Start** then **Run now** at `/monitor` |
| Next.js crashes on startup | Use `npm run dev -- --webpack` (Turbopack not supported on darwin/arm64) |
