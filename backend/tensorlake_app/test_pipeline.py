"""Smoke-test the Tensorlake → backend → iMessage pipeline.

This skips the Browser-Use scrapers entirely (`dry_run=true` on the backend)
and just verifies that:

  1. A Tensorlake sandbox can reach the backend over your ngrok URL.
  2. The X-Tensorlake-Secret header is accepted.
  3. The backend forwards the stub markdown via Photon iMessage.

Usage:
    uv run python backend/tensorlake_app/test_pipeline.py
    uv run python backend/tensorlake_app/test_pipeline.py --recipient +15551234567
    uv run python backend/tensorlake_app/test_pipeline.py --local   # skip sandbox, hit localhost directly

Spin-up the first time creates an ephemeral sandbox + installs httpx.
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


def _build_payload(args) -> dict:
    body: dict = {
        "topic":     "pipeline-test",
        "platforms": ["twitter", "reddit", "linkedin"],
        "top_n":     1,
        "dry_run":   True,
    }
    recipient = args.recipient or os.environ.get("PULSE_RECIPIENT", "").strip()
    if recipient:
        body["recipient"] = recipient
    return body


def run_local(args) -> None:
    """Hit localhost:8000 directly — skips Tensorlake. Useful for isolating
    backend/iMessage failures from sandbox networking failures."""
    import httpx

    secret = os.environ.get("TENSORLAKE_WEBHOOK_SECRET", "").strip()
    if not secret:
        sys.exit("TENSORLAKE_WEBHOOK_SECRET not set")

    body = _build_payload(args)
    url  = "http://localhost:8000/api/social-pulse/tick"
    print(f"POST {url}\nbody: {json.dumps(body)}")
    r = httpx.post(url, json=body, headers={"X-Tensorlake-Secret": secret}, timeout=60)
    print(f"\n→ {r.status_code}")
    print(r.text)
    sys.exit(0 if r.is_success else 1)


def run_via_sandbox(args) -> None:
    from tensorlake.sandbox import Sandbox

    backend_url = os.environ.get("BACKEND_URL", "").strip()
    secret      = os.environ.get("TENSORLAKE_WEBHOOK_SECRET", "").strip()
    api_key     = os.environ.get("TENSORLAKE_API_KEY", "").strip()
    if not (backend_url and secret and api_key):
        sys.exit("BACKEND_URL, TENSORLAKE_WEBHOOK_SECRET, TENSORLAKE_API_KEY all required")

    body = _build_payload(args)
    url  = backend_url.rstrip("/") + "/api/social-pulse/tick"
    print(f"target: {url}")
    print(f"body:   {json.dumps(body)}\n")

    print("creating ephemeral sandbox...")
    sb = Sandbox.create(cpus=1.0, memory_mb=1024, timeout_secs=180)
    try:
        print("installing httpx...")
        install = sb.run(
            "python3",
            ["-m", "pip", "install", "httpx", "--quiet", "--break-system-packages"],
        )
        if install.stderr:
            stderr_safe = install.stderr.encode("ascii", "replace").decode("ascii")
            print("(install stderr)", stderr_safe[:400])

        # Inline-script approach so we don't have to write_file separately.
        # The body + secret are passed via env to keep them out of the script.
        script = (
            "import os, json, httpx, sys\n"
            "url    = os.environ['URL']\n"
            "secret = os.environ['SECRET']\n"
            "body   = json.loads(os.environ['BODY'])\n"
            "print(f'sandbox POST {url}', flush=True)\n"
            "r = httpx.post(url, json=body, headers={'X-Tensorlake-Secret': secret}, timeout=60)\n"
            "print(f'status={r.status_code}', flush=True)\n"
            "print(r.text, flush=True)\n"
            "sys.exit(0 if r.is_success else 1)\n"
        )

        print("running test from inside sandbox...\n")
        result = sb.run(
            "python3",
            ["-c", script],
            env={"URL": url, "SECRET": secret, "BODY": json.dumps(body)},
        )
        def _ascii(s: str | None) -> str:
            return (s or "").encode("ascii", "replace").decode("ascii")

        print("--- sandbox stdout ---")
        print(_ascii(result.stdout))
        if result.stderr:
            print("--- sandbox stderr ---")
            print(_ascii(result.stderr))
        sys.exit(0 if getattr(result, "exit_code", 0) == 0 else 1)
    finally:
        try:
            sb.terminate()
            print("sandbox terminated.")
        except Exception as exc:
            print(f"(sandbox terminate failed: {exc})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recipient", help="Override iMessage handle for this test")
    ap.add_argument("--local", action="store_true",
                    help="Skip Tensorlake sandbox; hit localhost:8000 directly")
    args = ap.parse_args()

    if args.local:
        run_local(args)
    else:
        run_via_sandbox(args)


if __name__ == "__main__":
    main()
