# How to run the Tensorlake → Photon social-pulse loop

End-to-end instructions for the every-10-minutes social-listening loop. A
named Tensorlake sandbox sleeps 600s in a loop, POSTs to the local FastAPI
backend over an ngrok tunnel, and the backend forwards the markdown summary
through the Photon iMessage SDK to a recipient.

```
sandbox `social-pulse-loop` (sleep 600s, repeat)
   |
   v   POST {ngrok}/api/social-pulse/tick   X-Tensorlake-Secret: <secret>
backend (FastAPI)
   |--> social_pulse.social_insights(topic, platforms, top_n)
   |       (Browser-Use scrapers + LLM summary)
   `--> imessage.send(recipient, markdown)         (macOS only)
```

State (`runs`, `last_status`, `last_error`, `last_run_at`) lives at
`/var/pulse/state.json` inside the sandbox, so it survives suspend/resume.

---

## 1. Prerequisites

| Tool | Why |
|------|-----|
| Backend running (`uv run uvicorn main:app`) | Receives the cron-fired POSTs |
| `ngrok` (free tier OK) | Public URL the sandbox can reach |
| Tensorlake account + API key | https://cloud.tensorlake.ai → API Keys |
| OpenAI API key | `social_insights` summarizer |
| Browser-Use API key + profile IDs | The four scrapers (skip with `dry_run`) |
| macOS host running uvicorn | Required for actual iMessage delivery |

The pipeline runs everywhere — but on Windows/Linux `imessage.send` returns
`False` and the tick still returns `delivered: false`. To actually deliver
iMessages, uvicorn must run on macOS.

---

## 2. Configure `backend/.env.local`

```dotenv
# Tensorlake
TENSORLAKE_API_KEY=tl_apiKey_...

# Authenticates POSTs to /api/social-pulse/tick. Generate with:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
TENSORLAKE_WEBHOOK_SECRET=<random>

# Public URL the sandbox will hit (your ngrok forwarding URL).
# Update this every time ngrok restarts on the free tier.
BACKEND_URL=https://your-tunnel.ngrok-free.dev

# Loop config — read by launch_loop.py at start time.
PULSE_TOPIC=openai
PULSE_TOP_N=3

# iMessage handle for the markdown summary. PULSE_RECIPIENT is consumed by
# launch_loop.py and baked into the sandbox config. TENSORLAKE_PULSE_RECIPIENT
# is the backend's fallback when the request body omits `recipient`.
PULSE_RECIPIENT=+15551234567
TENSORLAKE_PULSE_RECIPIENT=+15551234567

# Already required for the rest of the backend
OPENAI_API_KEY=sk-...
BROWSER_USE_API_KEY=bu_...
BROWSER_USE_TWITTER_PROFILE_ID=...
BROWSER_USE_REDDIT_PROFILE_ID=...
BROWSER_USE_LINKEDIN_PROFILE_ID=...
```

---

## 3. Bring up the stack

Three terminals:

**Terminal A — backend**

```powershell
cd backend
uv run uvicorn main:app --reload --port 8000
```

Wait for `Application startup complete.`

**Terminal B — ngrok**

```powershell
ngrok http 8000
```

Copy the `Forwarding` HTTPS URL into `BACKEND_URL` in `.env.local`.

**Terminal C — control**

This is where you'll run the launch / status / test commands.

---

## 4. Smoke test the pipeline (no Browser-Use spend)

The backend's `/api/social-pulse/tick` endpoint accepts a `dry_run: true`
flag. When set, it skips `social_insights` entirely and synthesizes a stub
markdown — perfect for verifying the wiring without firing the four
Browser-Use scrapers.

### 4a. Local-only (skip Tensorlake)

Hits the backend directly. Use this first to isolate
backend/iMessage failures from sandbox networking failures.

```powershell
uv run --project backend python backend\tensorlake_app\test_pipeline.py --local
```

Expected:

```
POST http://localhost:8000/api/social-pulse/tick
body: {"topic":"pipeline-test","platforms":[...],"top_n":1,"dry_run":true,"recipient":"+1..."}
-> 200
{"ok":true,"dry_run":true,"topic":"pipeline-test","items_total":0,"recipient":"+1...","delivered":false}
```

`delivered: false` on Windows/Linux is normal (no `osascript`). On macOS you
should see the test message land in Messages.app.

### 4b. Full Tensorlake round-trip

Spawns an ephemeral sandbox, installs `httpx`, POSTs through the ngrok URL,
prints the sandbox's stdout, then terminates the sandbox.

```powershell
uv run --project backend python backend\tensorlake_app\test_pipeline.py
```

Expected:

```
target: https://your-tunnel.ngrok-free.dev/api/social-pulse/tick
body:   {...,"dry_run": true,...}
creating ephemeral sandbox...
installing httpx...
running test from inside sandbox...
--- sandbox stdout ---
sandbox POST https://your-tunnel.ngrok-free.dev/api/social-pulse/tick
status=200
{"ok":true,"dry_run":true,...,"delivered":false}
sandbox terminated.
```

You should also see one GET in your ngrok terminal and a fresh row in the
Tensorlake dashboard.

---

## 5. Launch the every-10-min loop

Once the smoke test passes:

```powershell
uv run --project backend python backend\tensorlake_app\launch_loop.py
```

What this does:

1. Connects to (or creates) a **named** sandbox `social-pulse-loop`.
2. Uploads `/app/config.json` (topic, platforms, top_n, recipient, secret,
   backend URL) and `/app/loop.py` (the in-sandbox loop script).
3. Starts the loop with `sandbox.start_process("python3", ["/app/loop.py"])`
   so it runs detached.

The loop then runs forever:

```python
while True:
    httpx.post(f"{BACKEND_URL}/api/social-pulse/tick",
               json={"topic": ..., "platforms": [...], "top_n": 3},
               headers={"X-Tensorlake-Secret": SECRET}, timeout=300)
    save_state({...})
    time.sleep(600)
```

`launch_loop.py` is idempotent — running it again won't double-launch, it
detects an existing loop process and exits early.

### Operations

```powershell
# Show running PID + last state.json
uv run --project backend python backend\tensorlake_app\launch_loop.py --status

# Stream stdout from the loop
uv run --project backend python backend\tensorlake_app\launch_loop.py --tail

# SIGTERM the loop (sandbox stays alive)
uv run --project backend python backend\tensorlake_app\launch_loop.py --stop
```

To re-roll config (new topic, new ngrok URL, etc.): edit `.env.local`,
`--stop`, then run `launch_loop.py` again. It re-uploads `/app/config.json`
each launch.

---

## 6. Manual one-off POST

Useful for debugging without involving any sandbox:

```powershell
$body = @{
  topic = "openai"
  platforms = @("twitter","reddit","linkedin")
  top_n = 3
  dry_run = $false   # flip to $true to skip Browser-Use
} | ConvertTo-Json -Compress

Invoke-RestMethod `
  -Uri "http://localhost:8000/api/social-pulse/tick" `
  -Method Post `
  -ContentType "application/json" `
  -Headers @{ "X-Tensorlake-Secret" = "<your secret>" } `
  -Body $body `
  -TimeoutSec 300
```

Bash equivalent:

```bash
curl -X POST http://localhost:8000/api/social-pulse/tick \
  -H "Content-Type: application/json" \
  -H "X-Tensorlake-Secret: $TENSORLAKE_WEBHOOK_SECRET" \
  -d '{"topic":"openai","platforms":["twitter","reddit","linkedin"],"top_n":3}'
```

---

## 7. Troubleshooting

**`401 bad secret`**
  `X-Tensorlake-Secret` header doesn't match `TENSORLAKE_WEBHOOK_SECRET` in
  `.env.local`. Restart uvicorn after changing the env var.

**Backend timing out on first request**
  uvicorn reload is slow on Windows. Wait ~10s after a save and retry.

**`API error (status 400): Memory must be 1000-8192 MB per CPU core`**
  Tensorlake's minimum memory-per-core. Already set to 1024 MB in both
  `launch_loop.py` and `test_pipeline.py`. Bump if you change CPUs.

**`ModuleNotFoundError: No module named 'httpx'` inside the sandbox**
  Tensorlake sandboxes use PEP 668 (externally-managed). Install with
  `python3 -m pip install httpx --break-system-packages`. Already wired up.

**`delivered: false` even on macOS**
  Either `recipient` is missing (set `TENSORLAKE_PULSE_RECIPIENT` in env or
  pass `recipient` in the POST body) OR `imessage.send` failed — check
  uvicorn logs for "imessage send failed" warnings. The recipient handle
  must already exist in Messages.app.

**ngrok URL changed**
  Free tier reroll on every restart. Update `BACKEND_URL` in `.env.local`,
  `--stop` the loop, and re-launch.

**Empty Tensorlake dashboard**
  Means no sandbox has been created yet. Local-only tests don't touch the
  cloud. Run `test_pipeline.py` (without `--local`) or `launch_loop.py`.

---

## 8. File map

```
backend/
  main.py                                       # POST /api/social-pulse/tick
  imessage.py                                   # macOS osascript bridge
  social_pulse.py                               # social_insights(topic, ...)
  tensorlake_app/
    launch_loop.py                              # named-sandbox loop launcher
    test_pipeline.py                            # dry_run smoke test
    README.md                                   # short version of this doc
  .env.local                                    # secrets + config
```
