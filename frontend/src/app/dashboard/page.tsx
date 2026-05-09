"use client";

import { useEffect, useRef, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const MAX_CLIENT_MESSAGES = 200;

type Message = {
  id: string;
  text: string | null;
  participant: string | null;
  chatId: string | null;
  chatKind: "dm" | "group" | "unknown";
  service: string | null;
  createdAt: string;
};

type ConnectionState = "connecting" | "open" | "closed";

const initial = (handle: string | null) => {
  if (!handle) return "?";
  const trimmed = handle.replace(/[^a-z0-9]/gi, "");
  return (trimmed[0] ?? handle[0] ?? "?").toUpperCase();
};

const formatRelative = (iso: string, now: number) => {
  const diffSec = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
  if (diffSec < 5) return "just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return new Date(iso).toLocaleString();
};

export default function Dashboard() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [status, setStatus] = useState<ConnectionState>("connecting");
  const [now, setNow] = useState<number>(() => Date.now());
  const seenIds = useRef<Set<string>>(new Set());

  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(tick);
  }, []);

  useEffect(() => {
    const source = new EventSource(`${API_URL}/api/messages/stream`);

    source.addEventListener("open", () => setStatus("open"));
    source.addEventListener("error", () => setStatus("closed"));

    source.addEventListener("snapshot", (event) => {
      const batch = JSON.parse((event as MessageEvent).data) as Message[];
      const fresh = batch.filter((m) => !seenIds.current.has(m.id));
      for (const m of fresh) seenIds.current.add(m.id);
      setMessages((prev) =>
        [...fresh.reverse(), ...prev].slice(0, MAX_CLIENT_MESSAGES)
      );
    });

    source.addEventListener("message", (event) => {
      const incoming = JSON.parse(event.data) as Message;
      if (seenIds.current.has(incoming.id)) return;
      seenIds.current.add(incoming.id);
      setMessages((prev) => [incoming, ...prev].slice(0, MAX_CLIENT_MESSAGES));
    });

    return () => {
      source.close();
    };
  }, []);

  const dotClass =
    status === "open"
      ? "bg-emerald-500"
      : status === "connecting"
        ? "bg-amber-500"
        : "bg-rose-500";

  return (
    <main className="min-h-screen bg-zinc-50 p-6 font-sans text-zinc-900 dark:bg-black dark:text-zinc-100 sm:p-10">
      <div className="mx-auto flex max-w-3xl flex-col gap-6">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              Live iMessages
            </h1>
            <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
              {messages.length === 0
                ? "Waiting for messages…"
                : `${messages.length} message${messages.length === 1 ? "" : "s"} buffered`}
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-zinc-200 bg-white px-3 py-1.5 text-xs text-zinc-600 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-400">
            <span className={`h-2 w-2 rounded-full ${dotClass}`} />
            <span className="font-mono">{status}</span>
          </div>
        </header>

        {messages.length === 0 ? (
          <div className="rounded-lg border border-dashed border-zinc-300 bg-white p-12 text-center text-sm text-zinc-500 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-400">
            Send an iMessage to this Mac and it will appear here.
          </div>
        ) : (
          <ul className="flex flex-col gap-2">
            {messages.map((m) => (
              <li
                key={m.id}
                className="flex gap-3 rounded-lg border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-950"
              >
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-zinc-200 text-sm font-semibold text-zinc-700 dark:bg-zinc-800 dark:text-zinc-200">
                  {initial(m.participant)}
                </div>
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <div className="flex items-center gap-2 text-sm">
                    <span className="truncate font-medium text-zinc-900 dark:text-zinc-100">
                      {m.participant ?? "unknown"}
                    </span>
                    {m.chatKind === "group" && (
                      <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-xs font-medium text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300">
                        group
                      </span>
                    )}
                    <span className="ml-auto shrink-0 text-xs text-zinc-500 dark:text-zinc-500">
                      {formatRelative(m.createdAt, now)}
                    </span>
                  </div>
                  <p className="whitespace-pre-wrap break-words text-sm text-zinc-700 dark:text-zinc-300">
                    {m.text ?? <em className="text-zinc-400">(no text)</em>}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}
