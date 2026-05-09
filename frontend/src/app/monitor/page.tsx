"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// ── Types ─────────────────────────────────────────────────────────────────────

interface MonitorStatus {
  enabled: boolean;
  config: {
    brand_terms: string[];
    lookback_minutes: number;
    alert_to: string | null;
    interval_minutes: number;
  };
  last_run: RunSummary | null;
  next_run: string | null;
  total_seen: number;
  signal_threshold: number;
  quiet_runs: number;
  last_alert_at: string | null;
  tensorlake_active: boolean;
}

interface RunSummary {
  ts: string;
  terms_used: string[];
  new_mentions: number;
  high_signal: number;
  total_seen: number;
  threshold: number;
  quiet_runs: number;
  status: "ok" | "partial";
  error: string | null;
}

interface Mention {
  post_id: string;
  platform: "x" | "linkedin" | "reddit";
  url: string;
  author: string;
  text: string;
  likes: number;
  reposts: number;
  comments: number;
  terms: string[];
  ts: string;
}

interface HistoryData {
  mentions: Mention[];
  runs: RunSummary[];
  baselines: Record<string, number>;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function PlatformBadge({ platform }: { platform: string }) {
  const colors: Record<string, string> = {
    x: "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-black",
    linkedin: "bg-blue-600 text-white",
    reddit: "bg-orange-500 text-white",
  };
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${colors[platform] ?? "bg-zinc-400 text-white"}`}>
      {platform}
    </span>
  );
}

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span className={`inline-block h-2 w-2 rounded-full ${ok ? "bg-green-400 animate-pulse" : "bg-zinc-400"}`} />
  );
}

function RunRow({ run }: { run: RunSummary }) {
  const ts = new Date(run.ts);
  return (
    <div className="flex items-center gap-3 rounded-md bg-white px-3 py-2 text-sm shadow-sm dark:bg-zinc-900">
      <span className={`text-xs font-medium ${run.status === "ok" ? "text-green-500" : "text-yellow-500"}`}>
        {run.status.toUpperCase()}
      </span>
      <span className="text-zinc-500 dark:text-zinc-400 text-xs tabular-nums">
        {ts.toLocaleTimeString()}
      </span>
      <span className="flex-1 text-zinc-700 dark:text-zinc-300">
        {run.new_mentions} new · {run.high_signal} high-signal
      </span>
      <span className="text-xs text-zinc-400">
        threshold={run.threshold}
      </span>
      {run.quiet_runs > 0 && (
        <span className="text-xs text-amber-500">quiet×{run.quiet_runs}</span>
      )}
    </div>
  );
}

function MentionCard({ mention }: { mention: Mention }) {
  const engagement = mention.likes + mention.comments;
  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-3 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <div className="mb-1 flex items-center gap-2">
        <PlatformBadge platform={mention.platform} />
        <span className="text-xs text-zinc-500 dark:text-zinc-400">{mention.author}</span>
        <span className="ml-auto text-xs text-zinc-400 tabular-nums">
          {new Date(mention.ts).toLocaleString()}
        </span>
      </div>
      <p className="text-sm text-zinc-800 dark:text-zinc-200 leading-snug line-clamp-3">
        {mention.text}
      </p>
      <div className="mt-2 flex items-center gap-3 text-xs text-zinc-500">
        <span>👍 {mention.likes}</span>
        <span>💬 {mention.comments}</span>
        <span>🔁 {mention.reposts}</span>
        {engagement > 0 && (
          <span className="ml-auto font-medium text-emerald-600 dark:text-emerald-400">
            +{engagement} engagement
          </span>
        )}
        {mention.url && (
          <a
            href={mention.url}
            target="_blank"
            rel="noopener noreferrer"
            className="ml-2 underline text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-200"
          >
            view →
          </a>
        )}
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function MonitorPage() {
  const [status, setStatus] = useState<MonitorStatus | null>(null);
  const [history, setHistory] = useState<HistoryData | null>(null);
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [lastTriggerMs, setLastTriggerMs] = useState<number | null>(null);

  const [terms, setTerms] = useState("");
  const termsRef = useRef(terms);
  useEffect(() => { termsRef.current = terms; }, [terms]);
  const [intervalMin, setIntervalMin] = useState(15);
  const [lookbackMin, setLookbackMin] = useState(60);
  const [alertTo, setAlertTo] = useState("");

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/monitor/status`);
      if (r.ok) {
        const data: MonitorStatus = await r.json();
        setStatus(data);
        if (!termsRef.current && data.config && data.config.brand_terms.length > 0) {
          setTerms(data.config.brand_terms.join(", "));
          setIntervalMin(data.config.interval_minutes);
          setLookbackMin(data.config.lookback_minutes);
          setAlertTo(data.config.alert_to ?? "");
        }
      }
    } catch { /* backend not up yet */ }
  }, []); // stable — reads terms via ref to avoid re-registering interval on every keystroke

  const fetchHistory = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/monitor/history?limit=50`);
      if (r.ok) setHistory(await r.json());
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    fetchStatus();
    fetchHistory();
    pollRef.current = setInterval(() => {
      fetchStatus();
      fetchHistory();
    }, 10_000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [fetchStatus, fetchHistory]);

  async function saveConfig() {
    setSaving(true);
    try {
      const brand_terms = terms.split(",").map(t => t.trim()).filter(Boolean);
      await fetch(`${API}/api/monitor/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          brand_terms,
          interval_minutes: intervalMin,
          lookback_minutes: lookbackMin,
          alert_to: alertTo || null,
        }),
      });
      await fetchStatus();
    } finally {
      setSaving(false);
    }
  }

  async function triggerNow() {
    setRunning(true);
    const t0 = Date.now();
    try {
      await fetch(`${API}/api/monitor/trigger`, { method: "POST" });
      setLastTriggerMs(Date.now() - t0);
      await Promise.all([fetchStatus(), fetchHistory()]);
    } catch { /* ignore */ } finally {
      setRunning(false);
    }
  }

  const runs = history?.runs ?? [];
  const mentions = history?.mentions ?? [];
  const baselines = history?.baselines ?? {};

  return (
    <main className="min-h-screen bg-zinc-50 font-sans dark:bg-black text-zinc-900 dark:text-zinc-100">
      {/* Header */}
      <div className="border-b border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950 px-6 py-4 flex items-center gap-4">
        <div className="flex items-center gap-2">
          <StatusDot ok={status?.enabled ?? false} />
          <h1 className="text-lg font-semibold">Brand Monitor</h1>
          {status?.tensorlake_active && (
            <span className="rounded bg-violet-100 px-2 py-0.5 text-[11px] font-semibold text-violet-700 dark:bg-violet-900 dark:text-violet-300">
              Tensorlake
            </span>
          )}
        </div>
        {status && (
          <div className="ml-auto flex items-center gap-4 text-sm text-zinc-500 dark:text-zinc-400">
            <span>{status.total_seen.toLocaleString()} seen</span>
            <span>threshold={status.signal_threshold}</span>
            {status.next_run && (
              <span>next {new Date(status.next_run).toLocaleTimeString()}</span>
            )}
            {status.quiet_runs > 0 && (
              <span className="text-amber-500">quiet×{status.quiet_runs} → auto-expanding terms</span>
            )}
          </div>
        )}
        <a href="/dashboard" className="ml-4 text-sm text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-200">
          ← Dashboard
        </a>
      </div>

      <div className="mx-auto max-w-6xl px-6 py-8 grid grid-cols-1 lg:grid-cols-3 gap-6">

        {/* Left column — config + run history */}
        <div className="flex flex-col gap-6">

          {/* Config card */}
          <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5 shadow-sm">
            <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-zinc-500">
              Agent Config
            </h2>
            <label className="block mb-3">
              <span className="text-xs text-zinc-500 dark:text-zinc-400">Brand terms (comma-separated)</span>
              <input
                className="mt-1 w-full rounded-md border border-zinc-200 dark:border-zinc-700 bg-zinc-50 dark:bg-zinc-800 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"
                placeholder="acme, acme corp, @acme"
                value={terms}
                onChange={e => setTerms(e.target.value)}
              />
            </label>
            <div className="grid grid-cols-2 gap-3 mb-3">
              <label className="block">
                <span className="text-xs text-zinc-500 dark:text-zinc-400">Interval (min)</span>
                <input
                  type="number" min={1} max={1440}
                  className="mt-1 w-full rounded-md border border-zinc-200 dark:border-zinc-700 bg-zinc-50 dark:bg-zinc-800 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"
                  value={intervalMin}
                  onChange={e => setIntervalMin(Number(e.target.value))}
                />
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500 dark:text-zinc-400">Lookback (min)</span>
                <input
                  type="number" min={5} max={1440}
                  className="mt-1 w-full rounded-md border border-zinc-200 dark:border-zinc-700 bg-zinc-50 dark:bg-zinc-800 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"
                  value={lookbackMin}
                  onChange={e => setLookbackMin(Number(e.target.value))}
                />
              </label>
            </div>
            <label className="block mb-4">
              <span className="text-xs text-zinc-500 dark:text-zinc-400">iMessage alert recipient (optional)</span>
              <input
                className="mt-1 w-full rounded-md border border-zinc-200 dark:border-zinc-700 bg-zinc-50 dark:bg-zinc-800 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"
                placeholder="+1 555 000 0000"
                value={alertTo}
                onChange={e => setAlertTo(e.target.value)}
              />
            </label>
            <div className="flex gap-2">
              <button
                onClick={saveConfig}
                disabled={saving || !terms.trim()}
                className="flex-1 rounded-md bg-violet-600 px-4 py-2 text-sm font-medium text-white hover:bg-violet-700 disabled:opacity-50"
              >
                {saving ? "Saving…" : "Save & Start"}
              </button>
              <button
                onClick={triggerNow}
                disabled={running || !status?.enabled}
                className="rounded-md border border-zinc-200 dark:border-zinc-700 px-4 py-2 text-sm font-medium hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-50"
              >
                {running ? "Running…" : "Run now"}
              </button>
            </div>
            {lastTriggerMs !== null && (
              <p className="mt-2 text-xs text-zinc-400">Last manual run: {lastTriggerMs}ms</p>
            )}
          </div>

          {/* Baselines card */}
          {Object.keys(baselines).length > 0 && (
            <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5 shadow-sm">
              <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
                Adaptive Baselines
              </h2>
              <p className="mb-3 text-xs text-zinc-400">
                High-signal = ≥2× platform median. Learned from {status?.total_seen.toLocaleString()} observations.
              </p>
              {Object.entries(baselines).map(([platform, baseline]) => (
                <div key={platform} className="flex items-center justify-between py-1.5 text-sm">
                  <PlatformBadge platform={platform} />
                  <span className="text-zinc-600 dark:text-zinc-300">
                    median {baseline.toFixed(1)} · signal ≥{(baseline * 2).toFixed(0)}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* Run history */}
          <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5 shadow-sm">
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
              Run History
            </h2>
            {runs.length === 0 ? (
              <p className="text-sm text-zinc-400">No runs yet. Save config to start the agent.</p>
            ) : (
              <div className="flex flex-col gap-2">
                {runs.map((r, i) => <RunRow key={i} run={r} />)}
              </div>
            )}
          </div>
        </div>

        {/* Right column — mention feed */}
        <div className="lg:col-span-2 flex flex-col gap-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
              Mention Feed
            </h2>
            <span className="text-xs text-zinc-400">{mentions.length} mentions in memory</span>
          </div>

          {mentions.length === 0 ? (
            <div className="rounded-xl border border-dashed border-zinc-300 dark:border-zinc-700 p-10 text-center text-zinc-400">
              <p className="text-lg mb-1">🛰️</p>
              <p className="text-sm">Agent is listening. Mentions will appear here after the first run.</p>
              {!status?.enabled && (
                <p className="mt-2 text-xs text-zinc-500">Configure brand terms and click <strong>Save & Start</strong> to activate.</p>
              )}
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {mentions.map(m => <MentionCard key={m.post_id} mention={m} />)}
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
