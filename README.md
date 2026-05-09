# Sentinel

Live iMessage dashboard with an OpenAI reply agent and a 3D agent-mesh visualization.

## Architecture

```
 macOS iMessage          FastAPI                  Next.js
  chat.db (WAL)           backend                  /dashboard
       │                     │                         │
       ▼                     ▼                         ▼
 ┌──────────┐  POST    ┌──────────┐   SSE     ┌────────────────┐
 │ bridge/  │─────────▶│ /ingest  │──────────▶│ tabs + chat +  │
 │ (Bun)    │          │ + agent  │           │ 3D agent scene │
 └──────────┘          └──────────┘           └────────────────┘
                            │
                            ▼ inbound? → OpenAI → outbound message
```

- `bridge/` — Bun watcher using [`@photon-ai/imessage-kit`](https://github.com/photon-hq/imessage-kit). Tails `~/Library/Messages/chat.db` and POSTs each non-self message to the backend.
- `backend/` — FastAPI (managed by [uv](https://docs.astral.sh/uv/)). In-memory ring buffer + SSE stream + OpenAI reply agent triggered on every inbound message.
- `frontend/` — Next.js 16 (App Router, React 19, Tailwind 4). `/dashboard` shows closable participant tabs, a live chat thread (inbound + agent replies), and a `react-three-fiber` 3D scene of the agent mesh.

## One-time setup

### Prerequisites

- macOS with Messages.app signed in
- **Full Disk Access** for the terminal that will run the bridge (System Settings → Privacy & Security → Full Disk Access). The SDK reads `~/Library/Messages/chat.db`.
- [Bun ≥ 1.0](https://bun.sh), [uv](https://docs.astral.sh/uv/)
- An [OpenAI API key](https://platform.openai.com/api-keys) for the reply agent

### Configure the OpenAI key

```sh
cp backend/.env.example backend/.env.local
# then edit backend/.env.local and paste your key into OPENAI_API_KEY
```

The default model is `gpt-4o-mini` — override with `OPENAI_MODEL` if you want something else. `backend/.env.local` is gitignored.

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
bun install
bun run dev
```

**Bridge** (the iMessage watcher):

```sh
cd bridge
bun install
bun run start
```

Open http://localhost:3000/dashboard. Send an iMessage to your Mac and it will appear as a new tab; the agent's reply lands in the chat a couple of seconds later.

## Useful endpoints

- `GET /api/agent/status` — `{ configured: boolean }` — whether `OPENAI_API_KEY` is set
- `GET /api/conversations` — list of active (non-closed) conversations
- `GET /api/messages/stream` — Server-Sent Events stream of all messages (snapshot + live)
- `POST /api/conversations/:participant/close` — hide a tab (data is preserved, can reopen)
