# Browser-Use Cloud → Live Frontend Integration

**Date:** 2026-05-09
**Status:** Approved (brainstorm)

## Goal

When a `*_scroll.py` Browser-Use Cloud agent is running, render its live iframe
stream inside the corresponding platform slot in the 3D `AgentScene`. If the
slot is already occupied, spawn a new orbital node connected to the
Orchestrator core. Test with `query=anthropic` across LinkedIn (×2), X, and
Reddit.

## Architecture

```
[*_scroll.py] -> Browser-Use Cloud session (live_url)
       │
       │ run_scrape() now also writes to Convex via convex_client
       ▼
[scraperSessions]  +liveUrl  +cloudSessionId  runId optional
       │
       │ Convex live query
       ▼
[AgentScene] -> first session per platform fills the static slot;
                additional sessions spawn orbital nodes around core
```

## Convex schema (`frontend/convex/schema.ts`)

`scraperSessions` gains:
- `liveUrl?: string` — Browser-Use Cloud iframe URL
- `cloudSessionId?: string` — opaque BU session id (for cross-reference)
- `runId?: Id<"agentRuns">` (was required) — cloud scrolls run standalone

Indexes unchanged. The existing `by_run` index excludes rows with absent
`runId` automatically.

## Convex API (`frontend/convex/sessions.ts`)

Add:

```ts
export const startCloud = mutation({
  args: { platform, query, liveUrl, cloudSessionId },
  // status: "running", browserBacked: true, no runId
});

export const activeCloud = query({
  // returns running + starting rows where liveUrl !== undefined
});
```

`finish` mutation already handles cloud sessions unchanged.

## Backend (`backend/`)

### `convex_client.py`
- `start_cloud_session(*, platform, query, live_url, cloud_session_id) -> str`
- Reuses existing `finish_session(session_id, status, error?)`
- All Convex calls are best-effort: log + continue on failure so cloud scrolls
  remain runnable without `CONVEX_URL`

### `browser_use_common.py`
- `run_scrape(...)` gains optional kwarg `convex_platform: Literal["reddit","x","linkedin"] | None = None`
- After cloud session creation, if `live_url` and `convex_platform` are set,
  call `start_cloud_session(...)` and stash the returned id
- In `finally`, if a convex session was created, call `finish_session(...)`
  with status derived from the last polled task status (`complete` /
  `error`)

### `*_scroll.py` (linkedin / twitter / reddit)
- Pass `convex_platform="linkedin" | "x" | "reddit"` into `run_scrape`
- `tiktok_scroll.py` left untouched (out of scope)

## Frontend (`frontend/src/app/dashboard/_components/AgentScene.tsx`)

### Live session lookup
```ts
const cloudSessions = useQuery(api.sessions.activeCloud) ?? [];
const byPlatform = groupBy(cloudSessions, s => s.platform);
```

### Slot rendering
- For each `PLATFORMS[i]`: `slotSession = byPlatform.get(p.id)?.[0]`
- `<BrowserChrome>` accepts optional `liveUrl?: string`
- When set: replace the static color screen + glyph with an
  `<Html transform occlude>` iframe sized to fill the screen plane (~580×320
  CSS px scaled to 1.86×1.0 world units). Title bar / traffic lights stay.
  Address pill text becomes `live.browser-use.com`. A small `● LIVE` pill is
  added.
- When unset: existing behaviour.
- Iframe attrs: `sandbox="allow-scripts allow-same-origin"`, `loading="lazy"`,
  `referrerPolicy="no-referrer"`.

### Orbital extras
- `extras = [...byPlatform per-platform tail, ...byPlatform.get(unknown)]`
- Cap at 3 extras (so at most 6 live iframes total). Surplus → static
  placeholder node with "queued" label.
- Layout: deterministic ring around the core. For extra `i` of `N`:
  - `theta = 2π · i / max(N, 3)` (so spacing is sane even at N=1)
  - `radius = 4.2`, `y = 1.4 + (i % 2 === 0 ? 0 : -0.6)`
  - `position = [cos(theta) * radius, y, sin(theta) * radius]`
- A dashed line connects each extra to `(0,0,0)` mirroring the existing
  PlatformAssembly behaviour.
- Each extra rendered via the same `PlatformAssembly` (refactored to accept a
  `Platform` plus optional `liveUrl`).
- Label: `${platform} · ${query}`.

### Color/style for orbital extras
- Use the source platform's color/emissive when known; fall back to violet
  (`#a78bfa` / `#c4b5fd`) for unknown platforms.

## Failure modes

- `CONVEX_URL` unset in backend: cloud scroll logs `convex disabled`, runs
  normally, no DB writes.
- `live_url` is `None`: skip Convex write — slot stays static.
- Iframe blocked by remote CSP / X-Frame-Options: drei's `<Html>` shows the
  fallback DOM. We render a "● LIVE" badge with a clickable link to the
  live_url over the static panel.
- Backend restart: existing `abortAllRunning` mutation flips orphan rows to
  `error`; orphan slots vanish from `activeCloud` query.
- Concurrency: cap iframes at 6; surplus nodes show a "queued" placeholder.

## Test plan

1. `cd frontend && bunx convex dev --once` to push schema.
2. `cd frontend && bun run dev` and open `/dashboard`.
3. Backend `.env.local` has `BROWSER_USE_API_KEY` and `CONVEX_URL`.
4. From `backend/`, run four scrolls in parallel:
   - `uv run python linkedin_scroll.py --query anthropic --no-open`
   - `uv run python twitter_scroll.py --query anthropic --no-open`
   - `uv run python reddit_scroll.py --query anthropic --no-open`
   - `uv run python linkedin_scroll.py --query anthropic --no-open`  *(duplicate → orbital)*
5. Verify dashboard: 3 slots show their iframes; one extra orbital node spawns;
   labels reflect platform + query; lines route to core.
6. Stop scrolls one at a time; verify slot reverts to static / orbital
   disappears.

## Out of scope

- TikTok platform integration (no slot defined).
- Local-browser pipeline (`scraper_stream.py`) — unchanged.
- Auth flows (`--login-only`) — unchanged.
- Persisting historical session list (only "currently live" is rendered).
