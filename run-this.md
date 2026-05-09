# run-this.md — Mac setup guide

Hey! This is the one-shot guide to get the whole thing running on your Mac.
Two processes, one terminal each. Should take ~10 minutes.

> **What you're running:**
> 1. **`backend/`** — the FastAPI server. Hosts Rachel (the iMessage agent), the marketing-campaign pipeline, the Browser-Use scrapers, etc.
> 2. **`bridge/`** — a Bun script that watches your Mac's iMessage database and forwards every incoming message to the backend.
>
> **Why both have to run on your Mac:** the bridge uses macOS-only APIs to read iMessage, and the backend uses macOS-only AppleScript (`osascript`) to send replies back through Messages.app. If either lives on a different OS, the loop breaks.

---

## 0. Prereqs (one-time install)

```bash
# Python 3.13+ via uv (fast Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Bun (for the bridge)
curl -fsSL https://bun.sh/install | bash

# (optional) nia CLI for the campaign pipeline's local sync
# Skip if you don't have it — campaigns still work, you just won't have
# Nia chat over the data folder.
brew install nia-ai-llms/tap/nia   # or however it's distributed
```

Re-open your terminal after installing so the new commands are on PATH.

## 1. Grant iMessage access

macOS gates Messages.app access behind two permissions. Grant both:

1. **Full Disk Access** — System Settings → Privacy & Security → Full Disk Access → toggle ON for **Terminal** (or whatever terminal you use: iTerm, Warp, etc.)
2. **Automation** — System Settings → Privacy & Security → Automation → expand your terminal → toggle ON for **Messages**

Without (1) the bridge can't read your iMessage SQLite DB. Without (2) the backend can't send via AppleScript.

If you skip these, you'll see permission errors the first time messages flow.

## 2. Clone + install

```bash
git clone https://github.com/bhajneeks/nozomioABC.git
cd nozomioABC

# Backend deps (this also creates the venv)
cd backend
uv sync
cd ..

# Bridge deps
cd bridge
bun install
cd ..
```

## 3. Drop in the secrets

Get a copy of `backend/.env` from Clarissa (DM her). It looks like:

```ini
OPENAI_API_KEY=sk-...
REACHER_API_KEY=rk_live_...
REACHER_SHOP_ID=1079
OVERSHOOT_API_KEY=ovs_...
BROWSER_USE_API_KEY=bu_...
BROWSER_USE_TIKTOK_PROFILE_ID=...
BROWSER_USE_TWITTER_PROFILE_ID=...
BROWSER_USE_LINKEDIN_PROFILE_ID=...
NOTION_API_KEY=ntn_...
NOTION_SCRIPTS_PAGE_ID=35b2e5a1e6d480c0b3dffd9f5c67bfd8
TENSORLAKE_API_KEY=tl_apiKey_...
TENSORLAKE_WEBHOOK_SECRET=...
BACKEND_URL=https://<your-ngrok>.ngrok-free.dev
PULSE_TOPIC=openai
PULSE_TOP_N=3
PULSE_RECIPIENT=+1...
CONVEX_URL=https://...convex.cloud
```

Save it as `backend/.env` (gitignored — won't get committed). **Don't** put it anywhere else.

> **Notion note:** the integration is named "Nozomio" and Clarissa already shared the Scripts page with it. You don't need to re-share unless you want to publish to a different page.
>
> **Browser-Use note:** the profile IDs in the env are shared cookie jars hosted on Browser-Use cloud. You don't need to log into TikTok / X / LinkedIn yourself — the agents pick up the cookies automatically.

## 4. Bridge config

The bridge POSTs to wherever `BACKEND_URL` points. For local dev, the default is `http://localhost:8000` (and that's what we want). No env file needed for the bridge itself.

## 5. Start the backend (Terminal 1)

```bash
cd backend
uv run uvicorn main:app --reload --port 8000
```

You should see:
```
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:     Application startup complete.
```

Verify in another shell:
```bash
curl -sS http://localhost:8000/api/hello
# {"message":"f from FastAPI"}
```

## 6. Start the bridge (Terminal 2)

```bash
cd bridge
bun run index.ts
```

You should see:
```
bridge watching iMessage → http://localhost:8000/api/messages/ingest
```

The first time you receive a message, macOS may pop a permission prompt — click **OK**.

## 7. Test the loop

Have someone text your iMessage account. In **Terminal 1** (backend) you should see:
```
INFO:     127.0.0.1:... - "POST /api/messages/ingest HTTP/1.1" 204
```
And in **Terminal 2** (bridge):
```
forwarded: +1xxx (dm): hey can u track openai for me
```

Within ~3 seconds the sender should get a reply back from Rachel via iMessage.

## 8. Try both pipelines

Rachel auto-routes between two pipelines based on what you say:

### TRACKING (live company watching)
Trigger phrases: pasting a URL, "track X", "watch X", "monitor X", "keep tabs on X".

```
You:    yo can u track openai for me, https://openai.com
Rachel: got it || tracking openai from https://openai.com || got 4 browsers
        watching across linkedin, x, reddit, n tiktok
```

What happens behind the scenes:
- Persists a Nia-indexed file to `data/tracked/openai.md`
- Spins up 4 keep-alive Browser-Use cloud sessions (LinkedIn, X, Reddit, TikTok)
- Each watcher loops, finds opinion-bearing posts, and DMs you the insight

Re-tracking the same company **does not** create a duplicate file — it appends a new entry under `## Runs` in the existing one.

If you express an opinion ("ngl their pricing is sus"), Rachel quietly calls `note_user_comment` and appends it to the same file under `## User comments`. Build up your voice over time.

Ask "what are you tracking?" anytime to get the list back.

### MARKETING CAMPAIGN (one-shot brief → campaign markdown)
Trigger phrases: "make a campaign", "marketing campaign", "build me a campaign for X".

```
You:    make me a marketing campaign for hydrating tinted lip oil
Rachel: on it, building it now || takes a min, ill ping u
        (~60s later)
Rachel: done || called it 'Glide Test Lip Oil' || saved + scripts in notion
```

What happens behind the scenes:
- Reacher subagents pull TikTok Shop competitor products + creators + trending hooks
- (Optional) Browser-Use scrapers pull live social posts on the topic
- LLM synthesizes a campaign markdown to `data/campaigns/<ts>-<slug>.md`
- Generates 3 creator-ready video scripts
- (Optional) publishes them to the Notion Scripts page
- Proposes DM-automation payloads through Reacher (gated by env vars; off by default)

## 9. Dashboards (optional)

If you want a visual:
```bash
cd frontend
bun install
bun run dev
# open http://localhost:3000/dashboard
```

You'll see live message threads + per-platform browser sessions when tracking is active.

## 10. Troubleshooting

**"command not found: uv" or "command not found: bun"** — re-open your terminal after the curl install. If still missing: `which uv` should give `~/.cargo/bin/uv`. If not, add `~/.cargo/bin` to PATH.

**Bridge logs `permission denied` reading messages DB** — Full Disk Access not granted (Step 1).

**Backend logs `imessage send failed` or `osascript ... not authorized`** — Automation permission for Messages not granted (Step 1).

**Backend startup error `OPENAI_API_KEY is not set`** — `.env` not in `backend/` or has typos.

**Notion publish fails with 404** — the Scripts page isn't shared with the "Nozomio" integration. Open the page → `…` → Connections → Add Nozomio.

**Reacher write returns 403 `WRITE_NOT_PERMITTED`** — the Reacher API key is read-only. The campaign pipeline still works end-to-end and returns the planned DM payload — it just can't actually create automations until the key is upgraded. Keep `AUTOMATIONS_ENABLED=false` (default) and you'll never hit this path.

**Rachel's reply is delayed by 30+ seconds** — usually the first call after server start triggers `track_company` which spins up 4 Browser-Use sessions in parallel. After that, replies are <3s. The marketing-campaign tool also takes 30–120s by design (multiple subagents in parallel).

**Replies show up in dashboard but not in iMessage** — Step 1 Automation perm. Or you started uvicorn outside the same terminal that has the perm; restart in the right terminal.

---

That's it. If anything's wonky, ping Clarissa with the full backend logs (the lines after the failing request) and she'll triage.
