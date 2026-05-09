"""Launch the every-10-minutes social-pulse loop inside a Tensorlake sandbox.

Idempotent: connects to the named sandbox if it already exists, otherwise
creates it. Uploads the loop script, makes sure httpx is available, then
starts it as a detached process.

The loop runs forever inside the sandbox — it sleeps 600s, POSTs to
{BACKEND_URL}/api/social-pulse/tick (over your ngrok tunnel), and lets the
backend run social_insights + iMessage delivery. State (last response, run
count) lives in /var/pulse/state.json so it survives sandbox suspend/resume.

Usage:
    uv run python backend/tensorlake_app/launch_loop.py            # start
    uv run python backend/tensorlake_app/launch_loop.py --status   # list processes
    uv run python backend/tensorlake_app/launch_loop.py --tail     # follow output
    uv run python backend/tensorlake_app/launch_loop.py --stop     # kill the loop
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env.local")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from tensorlake.sandbox import Sandbox

SANDBOX_NAME = "social-pulse-loop"
LOOP_PATH = "/app/loop.py"

# ── Loop script that runs forever inside the sandbox ──────────────────────────
# Reads BACKEND_URL / SECRET / topic config from /app/config.json (uploaded
# fresh each launch) so we don't bake secrets into the script body.
LOOP_SCRIPT = r"""
import json, os, sys, time, traceback
from datetime import datetime, timezone
from pathlib import Path

import httpx

CONFIG_PATH = Path("/app/config.json")
STATE_PATH  = Path("/var/pulse/state.json")
INTERVAL    = 600  # seconds — every 10 minutes


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"runs": 0, "last_status": None, "last_error": None, "last_run_at": None}


def save_state(s):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, default=str))
    tmp.replace(STATE_PATH)


def tick(cfg, state):
    body = {
        "topic":     cfg["topic"],
        "platforms": cfg["platforms"],
        "top_n":     cfg["top_n"],
    }
    if cfg.get("recipient"):
        body["recipient"] = cfg["recipient"]

    url = cfg["backend_url"].rstrip("/") + "/api/social-pulse/tick"
    print(f"[{datetime.now(timezone.utc).isoformat()}] POST {url}", flush=True)
    r = httpx.post(
        url,
        json=body,
        headers={"X-Tensorlake-Secret": cfg["secret"]},
        timeout=300,  # social_insights fans out 3-4 Browser-Use scrapers
    )
    state["last_status"]  = r.status_code
    state["last_run_at"]  = datetime.now(timezone.utc).isoformat()
    state["last_error"]   = None if r.is_success else r.text[:300]
    state["runs"]         = state.get("runs", 0) + 1
    print(f"  → {r.status_code}  body={r.text[:200]}", flush=True)


cfg   = json.loads(CONFIG_PATH.read_text())
state = load_state()
print(f"social-pulse loop starting — interval={INTERVAL}s, topic={cfg['topic']}", flush=True)

while True:
    try:
        tick(cfg, state)
    except Exception as exc:
        state["last_status"] = None
        state["last_error"]  = f"{type(exc).__name__}: {exc}"
        state["last_run_at"] = datetime.now(timezone.utc).isoformat()
        state["runs"]        = state.get("runs", 0) + 1
        print(f"  ! {state['last_error']}", flush=True)
        traceback.print_exc()
    save_state(state)
    time.sleep(INTERVAL)
"""


def get_or_create_sandbox() -> Sandbox:
    try:
        sb = Sandbox.connect(SANDBOX_NAME)
        print(f"connected to existing sandbox '{SANDBOX_NAME}'")
        return sb
    except Exception:
        print(f"creating sandbox '{SANDBOX_NAME}'")
        sb = Sandbox.create(name=SANDBOX_NAME, cpus=1.0, memory_mb=1024)
        sb.run("python3", ["-m", "pip", "install", "httpx", "--quiet", "--break-system-packages"])
        return sb


def upload_config_and_script(sb: Sandbox) -> None:
    backend_url = os.environ.get("BACKEND_URL", "").strip()
    secret      = os.environ.get("TENSORLAKE_WEBHOOK_SECRET", "").strip()
    if not backend_url or not secret:
        sys.exit("BACKEND_URL and TENSORLAKE_WEBHOOK_SECRET must be set in backend/.env.local")

    cfg = {
        "backend_url": backend_url,
        "secret":      secret,
        "topic":       os.environ.get("PULSE_TOPIC", "openai"),
        "platforms":   ["twitter", "reddit", "linkedin"],
        "top_n":       int(os.environ.get("PULSE_TOP_N", "3")),
        "recipient":   os.environ.get("PULSE_RECIPIENT", "").strip() or None,
    }
    sb.write_file("/app/config.json", json.dumps(cfg).encode())
    sb.write_file(LOOP_PATH, LOOP_SCRIPT.encode())
    print(f"uploaded config (topic={cfg['topic']}, recipient={cfg['recipient'] or '∅'})")


def find_loop_process(sb: Sandbox):
    try:
        for p in sb.list_processes():
            cmd = " ".join(getattr(p, "args", []) or [])
            if LOOP_PATH in cmd:
                return p
    except Exception as exc:
        print(f"list_processes failed: {exc}")
    return None


def cmd_start(sb: Sandbox) -> None:
    upload_config_and_script(sb)
    existing = find_loop_process(sb)
    if existing:
        print(f"loop already running — pid={existing.pid}")
        return
    proc = sb.start_process("python3", [LOOP_PATH])
    print(f"loop started — pid={proc.pid}")


def cmd_status(sb: Sandbox) -> None:
    proc = find_loop_process(sb)
    if proc:
        print(f"running — pid={proc.pid}")
    else:
        print("not running")
    try:
        out = sb.run("sh", ["-lc", "cat /var/pulse/state.json 2>/dev/null || echo '{}'"])
        print("state:", out.stdout.strip())
    except Exception as exc:
        print(f"state read failed: {exc}")


def cmd_tail(sb: Sandbox) -> None:
    proc = find_loop_process(sb)
    if not proc:
        sys.exit("loop is not running — start it first")
    print(f"following pid={proc.pid} (ctrl-C to detach)")
    sb.follow_output(proc.pid)


def cmd_stop(sb: Sandbox) -> None:
    proc = find_loop_process(sb)
    if not proc:
        print("loop is not running")
        return
    sb.run("sh", ["-lc", f"kill {proc.pid}"])
    print(f"sent SIGTERM to pid={proc.pid}")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true")
    g.add_argument("--tail",   action="store_true")
    g.add_argument("--stop",   action="store_true")
    args = ap.parse_args()

    if not os.environ.get("TENSORLAKE_API_KEY"):
        sys.exit("TENSORLAKE_API_KEY not set")

    sb = get_or_create_sandbox()
    if args.status:
        cmd_status(sb)
    elif args.tail:
        cmd_tail(sb)
    elif args.stop:
        cmd_stop(sb)
    else:
        cmd_start(sb)


if __name__ == "__main__":
    main()
