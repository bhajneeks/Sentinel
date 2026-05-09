"""
Brand monitoring agent — powered by Tensorlake Sandboxes.

Each run connects to a named Tensorlake sandbox ("brand-monitor-prod").
The sandbox filesystem persists between invocations — seen_ids, mention
history, and adaptive thresholds all survive restarts there.
"""
import asyncio
import atexit
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import monitor_store as local_store

logger = logging.getLogger("uvicorn.error")

SANDBOX_NAME = "brand-monitor-prod"
_executor = ThreadPoolExecutor(max_workers=2)
atexit.register(_executor.shutdown, wait=False)

# ── Self-contained script that executes INSIDE the Tensorlake sandbox ─────────
# Uses only httpx (lightweight, pre-installable).
# Reads/writes /var/monitor/state.json — Tensorlake snapshots this between runs.
_SANDBOX_SCRIPT = r"""
import json, httpx, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

STATE_PATH = Path("/var/monitor/state.json")
MAX_SEEN    = 20_000
MAX_HISTORY = 2_000


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"seen_ids": [], "history": [], "runs": 0, "signal_threshold": 5, "quiet_runs": 0}


def save_state(s):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, default=str))
    tmp.replace(STATE_PATH)


def scrape_reddit(terms, lookback_minutes, seen_ids):
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    out = []
    for term in terms:
        try:
            r = httpx.get(
                "https://www.reddit.com/search.json",
                params={"q": term, "sort": "new", "limit": 25},
                headers={"User-Agent": "brand-monitor/1.0"},
                timeout=10,
            )
            r.raise_for_status()
            for child in r.json().get("data", {}).get("children", []):
                d = child["data"]
                post_id = d.get("id", "")
                if post_id in seen_ids:
                    continue
                created = datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc)
                if created < cutoff:
                    continue
                out.append({
                    "post_id":  post_id,
                    "platform": "reddit",
                    "url":      "https://reddit.com" + d.get("permalink", ""),
                    "author":   d.get("author", ""),
                    "text":     (d.get("title", "") + " " + d.get("selftext", ""))[:500].strip(),
                    "likes":    d.get("score", 0),
                    "comments": d.get("num_comments", 0),
                    "reposts":  0,
                    "ts":       created.isoformat(),
                })
        except Exception as e:
            print(f"[reddit-error] {term}: {e}", file=sys.stderr, flush=True)
    return out


# ── Main ──────────────────────────────────────────────────────────────────────
cfg        = json.loads(sys.argv[1])
terms      = cfg["brand_terms"]
lookback   = cfg.get("lookback_minutes", 60)
quiet_runs = cfg.get("quiet_runs", 0)

# Plan: auto-expand terms when agent has been quiet for ≥3 consecutive runs
if quiet_runs >= 3:
    extras = [w for t in terms for w in t.split() if len(t.split()) > 1]
    terms  = list(dict.fromkeys(terms + extras))

state    = load_state()
seen_ids = set(state["seen_ids"])

mentions     = scrape_reddit(terms, lookback, seen_ids)
new_mentions = [m for m in mentions if m["post_id"] not in seen_ids]

threshold   = state.get("signal_threshold", 5)
high_signal = [m for m in new_mentions if m["likes"] + m["comments"] >= threshold]

# Adapt threshold
if high_signal:
    avg        = sum(m["likes"] + m["comments"] for m in high_signal) / len(high_signal)
    threshold  = max(5, int((threshold + avg * 0.5) / 1.5))
    quiet_runs = 0
else:
    threshold  = max(3, threshold - 1)
    quiet_runs = quiet_runs + 1

new_ids          = {m["post_id"] for m in new_mentions}
state["seen_ids"]        = list((seen_ids | new_ids))[-MAX_SEEN:]
state["history"]         = (state.get("history", []) + new_mentions)[-MAX_HISTORY:]
state["runs"]            = state.get("runs", 0) + 1
state["signal_threshold"] = threshold
state["quiet_runs"]       = quiet_runs

save_state(state)

# Only print JSON on the last line so the outer reader can parse it cleanly
print(json.dumps({
    "new_mentions": len(new_mentions),
    "high_signal":  high_signal[:10],
    "total_seen":   len(state["seen_ids"]),
    "threshold":    threshold,
    "quiet_runs":   quiet_runs,
    "run":          state["runs"],
}), flush=True)
"""


# ── Tensorlake sandbox helpers ────────────────────────────────────────────────

def _get_sandbox():
    """Connect to the named sandbox, creating it on first call."""
    from tensorlake.sandbox import Sandbox
    try:
        sb = Sandbox.connect(SANDBOX_NAME)
        logger.info("monitor: connected to existing sandbox '%s'", SANDBOX_NAME)
        return sb
    except Exception:
        logger.info("monitor: creating new sandbox '%s'", SANDBOX_NAME)
        sb = Sandbox.create(
            name=SANDBOX_NAME,
            cpus=1.0,
            memory_mb=1024,
            # No timeout_secs — sandbox persists indefinitely for durable memory
        )
        # One-time setup — httpx persists in sandbox filesystem after this
        sb.run("sh", ["-lc", "pip install httpx --quiet 2>&1"])
        return sb


def _sync_run_in_sandbox(brand_terms: list[str], lookback_minutes: int, quiet_runs: int) -> dict:
    """Synchronous — runs in a thread executor to avoid blocking the event loop."""
    sandbox = _get_sandbox()

    # Upload the monitoring script (idempotent — safe to overwrite each run)
    sandbox.write_file("/app/monitor.py", _SANDBOX_SCRIPT.encode())

    config = json.dumps({
        "brand_terms":      brand_terms,
        "lookback_minutes": lookback_minutes,
        "quiet_runs":       quiet_runs,
    })
    result = sandbox.run("python3", ["/app/monitor.py", config])

    if result.stderr:
        for line in result.stderr.splitlines():
            if "[reddit-error]" in line:
                logger.warning("monitor sandbox: %s", line)
            else:
                logger.debug("monitor sandbox stderr: %s", line)

    # Find the JSON line in stdout
    json_lines = [l for l in result.stdout.strip().splitlines() if l.startswith("{")]
    if not json_lines:
        raise RuntimeError(
            f"No JSON output from sandbox.\nstdout: {result.stdout[:300]}\nstderr: {result.stderr[:300]}"
        )
    return json.loads(json_lines[-1])


# ── Main async entry point ────────────────────────────────────────────────────

async def _run(
    brand_terms: list[str],
    lookback_minutes: int,
    alert_to: str | None,
    expand_if_quiet: bool = True,
) -> dict:
    api_key    = os.getenv("TENSORLAKE_API_KEY", "")
    state      = local_store.load()
    quiet_runs = state.get("quiet_runs", 0) if expand_if_quiet else 0
    run_ts     = datetime.now(timezone.utc)
    error_note: str | None = None

    errors: list[str] = []

    if not api_key:
        logger.warning("monitor: TENSORLAKE_API_KEY not set — set it in .env.local")
        return {"error": "TENSORLAKE_API_KEY not configured", "new_mentions": 0, "high_signal": []}

    try:
        output = await asyncio.get_running_loop().run_in_executor(
            _executor, _sync_run_in_sandbox, brand_terms, lookback_minutes, quiet_runs
        )
    except Exception as exc:
        logger.error("monitor: sandbox run failed: %s", exc)
        errors.append(str(exc)[:200])
        output = {
            "new_mentions": 0, "high_signal": [], "total_seen": 0,
            "threshold": state.get("signal_threshold", 5),
            "quiet_runs": quiet_runs + 1, "run": 0,
        }

    if alert_to and output.get("high_signal"):
        try:
            await _send_alert(alert_to, output["high_signal"], brand_terms)
            state["last_alert_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            logger.warning("monitor [alert]: %s", exc)
            errors.append(f"alert: {exc}")

    # Sync sandbox run result to local store so the dashboard can display it
    run_summary = {
        "ts":           run_ts.isoformat(),
        "terms_used":   brand_terms,
        "new_mentions": output["new_mentions"],
        "high_signal":  len(output.get("high_signal", [])),
        "total_seen":   output["total_seen"],
        "threshold":    output["threshold"],
        "quiet_runs":   output["quiet_runs"],
        "status":       "ok" if not errors else "partial",
        "error":        " | ".join(errors) or None,
    }
    state["quiet_runs"]      = output["quiet_runs"]
    state["signal_threshold"] = output["threshold"]
    state["runs"]            = (state.get("runs", []) + [run_summary])[-local_store.MAX_RUNS:]
    # Mirror high-signal mentions to local history for the dashboard feed
    state["history"]         = (
        state.get("history", []) + output.get("high_signal", [])
    )[-local_store.MAX_HISTORY:]
    local_store.save(state)

    logger.info(
        "monitor: run #%d — %d new, %d high-signal, threshold=%d, quiet=%d, sandbox=%s",
        output["run"], output["new_mentions"], len(output.get("high_signal", [])),
        output["threshold"], output["quiet_runs"],
        "✓" if api_key else "✗",
    )
    return run_summary


# ── Alert composition ─────────────────────────────────────────────────────────

async def _send_alert(recipient: str, mentions: list[dict], original_terms: list[str]) -> None:
    import imessage
    n      = len(mentions)
    header = f"Brand monitor [{', '.join(original_terms)}]: {n} new high-signal mention{'s' if n != 1 else ''}"
    lines  = [header]
    for m in mentions[:4]:
        snippet = m["text"][:120].replace("\n", " ")
        lines.append(
            f"\n• [{m['platform']}] {snippet}…"
            f"\n  👍 {m['likes']}  💬 {m['comments']}"
            f"\n  {m['url']}"
        )
    if n > 4:
        lines.append(f"\n…and {n - 4} more.")
    await imessage.send(recipient, "\n".join(lines))
