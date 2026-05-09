# Tensorlake → Photon social-pulse loop

A long-running Tensorlake sandbox loops every 10 minutes: it POSTs to the
local FastAPI backend (over ngrok), which runs `social_pulse.social_insights`
and forwards the markdown summary through the Photon iMessage bridge.

```
sandbox `social-pulse-loop` (sleeps 600s in a loop)
   └─► POST {ngrok}/api/social-pulse/tick   (X-Tensorlake-Secret header)
          └─► social_insights(topic, platforms, top_n)
          └─► imessage.send(recipient, markdown)
```

State (`runs`, `last_status`, `last_error`, `last_run_at`) lives at
`/var/pulse/state.json` inside the sandbox, so it survives suspend/resume.

## One-time setup

1. **Expose the backend with ngrok**:

   ```bash
   ngrok http 8000
   ```

   Copy the HTTPS forwarding URL.

2. **Fill `backend/.env.local`**:

   ```
   TENSORLAKE_API_KEY=tl_apiKey_...
   TENSORLAKE_WEBHOOK_SECRET=<random string — auths the cron-fired POSTs>
   BACKEND_URL=https://abcd-1234.ngrok-free.app
   PULSE_TOPIC=openai
   PULSE_TOP_N=3
   PULSE_RECIPIENT=+15551234567   # optional, iMessage handle
   ```

3. **Start uvicorn** (`uv run uvicorn main:app --reload --port 8000`).

4. **Launch the loop**:

   ```bash
   uv run python backend/tensorlake_app/launch_loop.py
   ```

   The first run creates the sandbox + installs `httpx`. Subsequent runs
   reconnect to the same `social-pulse-loop` sandbox — idempotent.

## Operations

```bash
uv run python backend/tensorlake_app/launch_loop.py --status   # PID + last state.json
uv run python backend/tensorlake_app/launch_loop.py --tail     # stream stdout
uv run python backend/tensorlake_app/launch_loop.py --stop     # SIGTERM the loop
```

## Manual smoke test

```bash
curl -X POST $BACKEND_URL/api/social-pulse/tick \
  -H "Content-Type: application/json" \
  -H "X-Tensorlake-Secret: $TENSORLAKE_WEBHOOK_SECRET" \
  -d '{"topic":"openai","platforms":["twitter","reddit","linkedin"],"top_n":3}'
```
