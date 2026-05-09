# nozomioABC

Next.js frontend + FastAPI backend.

## Layout

- `backend/` — FastAPI (managed by [uv](https://docs.astral.sh/uv/)), serves `GET /api/hello`
- `frontend/` — Next.js 16 (App Router, TypeScript, Tailwind 4), fetches the endpoint server-side

## Run

Open two terminals.

**Backend** (port 8000):

```sh
cd backend
uv run uvicorn main:app --reload --port 8000
```

**Frontend** (port 3000):

```sh
cd frontend
npm run dev
```

Then visit http://localhost:3000 — the page renders the message returned by the FastAPI endpoint.
