"""
Durable state store for the brand monitoring agent.

In Tensorlake cloud this JSON file lives inside a MicroVM sandbox filesystem
that is snapshotted between invocations — true cross-run persistence without
an external database. Locally it writes to MONITOR_STATE_PATH.

Removing this file between runs breaks deduplication entirely: the agent
re-alerts on every previously-seen mention, which demonstrates why stateful
memory is load-bearing.
"""
import json
import os
import statistics
from pathlib import Path

STATE_PATH = Path(os.getenv("MONITOR_STATE_PATH", "/tmp/brand_monitor_state.json"))

MAX_HISTORY = 2000
MAX_SEEN_IDS = 20_000
MAX_RUNS = 100
BASELINE_WINDOW = 200  # rolling data points per platform


def load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty()


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, default=str))
    tmp.replace(STATE_PATH)  # atomic write — never leaves a half-written file


def _empty() -> dict:
    return {
        "seen_ids": [],
        "history": [],
        "runs": [],
        "baselines": {},
        "signal_threshold": 5,
        "quiet_runs": 0,
        "last_alert_at": None,
    }


# ── Baseline helpers ──────────────────────────────────────────────────────────

def update_baselines(state: dict, mentions: list[dict]) -> dict:
    """Extend rolling per-platform engagement windows with new observations."""
    for m in mentions:
        platform = m.get("platform", "unknown")
        engagement = (m.get("likes") or 0) + (m.get("comments") or 0)
        window: list = state["baselines"].setdefault(platform, [])
        window.append(float(engagement))
        state["baselines"][platform] = window[-BASELINE_WINDOW:]
    return state


def platform_baseline(state: dict, platform: str) -> float:
    window = state["baselines"].get(platform, [])
    return statistics.median(window) if window else 0.0


def is_high_signal(mention: dict, state: dict) -> bool:
    """
    A mention is high-signal when its engagement exceeds both:
      - 2× the rolling median for its platform  (relative signal)
      - the absolute signal_threshold            (floor)

    Both thresholds adapt over time, so "high-signal" means something
    increasingly precise as the agent accumulates history.
    """
    platform = mention.get("platform", "unknown")
    engagement = (mention.get("likes") or 0) + (mention.get("comments") or 0)
    baseline = platform_baseline(state, platform)
    floor = state.get("signal_threshold", 5)
    return engagement >= max(baseline * 2, floor)
