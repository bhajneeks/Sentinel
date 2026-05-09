"use client";

import { ConvexProvider, useQuery } from "convex/react";
import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../../convex/_generated/api";
import { convex } from "../../lib/convex";
import ChatThread from "./_components/ChatThread";
import Tabs from "./_components/Tabs";
import type {
  ConnectionState,
  Conversation,
  Message,
  MentionPayload,
  Platform,
} from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const MAX_CLIENT_MESSAGES = 500;
const THINKING_TIMEOUT_MS = 30_000;

const AgentScene = dynamic(() => import("./_components/AgentScene"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-xs text-violet-300/50">
      booting agent mesh…
    </div>
  ),
});

export default function Dashboard() {
  return (
    <ConvexProvider client={convex}>
      <DashboardInner />
    </ConvexProvider>
  );
}

function DashboardInner() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeParticipant, setActiveParticipant] = useState<string | null>(null);
  const [status, setStatus] = useState<ConnectionState>("connecting");
  const [thinkingFor, setThinkingFor] = useState<Set<string>>(new Set());
  const [agentConfigured, setAgentConfigured] = useState<boolean | null>(null);

  const seenIds = useRef<Set<string>>(new Set());
  const closedLocally = useRef<Set<string>>(new Set());
  const thinkingTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map()
  );

  const upsertMessage = useCallback(
    (incoming: Message) => {
      if (seenIds.current.has(incoming.id)) return;
      seenIds.current.add(incoming.id);

      setMessages((prev) =>
        [incoming, ...prev].slice(0, MAX_CLIENT_MESSAGES)
      );

      if (incoming.participant && !closedLocally.current.has(incoming.participant)) {
        setConversations((prev) => updateConversations(prev, incoming));
      }

      if (!incoming.participant) return;

      if (incoming.direction === "inbound") {
        startThinking(incoming.participant);
      } else {
        stopThinking(incoming.participant);
      }
    },
    [],
  );

  const startThinking = (participant: string) => {
    setThinkingFor((prev) => {
      const next = new Set(prev);
      next.add(participant);
      return next;
    });
    const existing = thinkingTimers.current.get(participant);
    if (existing) clearTimeout(existing);
    const timer = setTimeout(() => stopThinking(participant), THINKING_TIMEOUT_MS);
    thinkingTimers.current.set(participant, timer);
  };

  const stopThinking = (participant: string) => {
    setThinkingFor((prev) => {
      if (!prev.has(participant)) return prev;
      const next = new Set(prev);
      next.delete(participant);
      return next;
    });
    const timer = thinkingTimers.current.get(participant);
    if (timer) {
      clearTimeout(timer);
      thinkingTimers.current.delete(participant);
    }
  };

  useEffect(() => {
    fetch(`${API_URL}/api/agent/status`)
      .then((res) => res.json())
      .then((data: { configured: boolean }) => setAgentConfigured(data.configured))
      .catch(() => setAgentConfigured(false));
  }, []);

  useEffect(() => {
    fetch(`${API_URL}/api/conversations`)
      .then((res) => res.json())
      .then((data: Conversation[]) => {
        const fresh = data.filter(
          (c) => !closedLocally.current.has(c.participant)
        );
        setConversations(fresh);
        setActiveParticipant((prev) => prev ?? fresh[0]?.participant ?? null);
      })
      .catch(() => {
        // Network fail: SSE snapshot will still populate.
      });
  }, []);

  useEffect(() => {
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const handleSnapshot = (event: Event) => {
      const batch = JSON.parse((event as MessageEvent).data) as Message[];
      const fresh = batch.filter((m) => {
        if (seenIds.current.has(m.id)) return false;
        seenIds.current.add(m.id);
        return true;
      });
      if (fresh.length === 0) return;
      setMessages((prev) =>
        [...fresh.reverse(), ...prev].slice(0, MAX_CLIENT_MESSAGES),
      );
      setConversations((prev) => {
        let next = prev;
        for (const m of fresh) {
          if (m.participant && !closedLocally.current.has(m.participant)) {
            next = updateConversations(next, m);
          }
        }
        return next;
      });
      setActiveParticipant((prev) => {
        if (prev) return prev;
        const first = fresh.find(
          (m) => m.participant && !closedLocally.current.has(m.participant),
        );
        return first?.participant ?? null;
      });
    };

    const handleMessage = (event: MessageEvent) => {
      const incoming = JSON.parse(event.data) as Message;
      upsertMessage(incoming);
      setActiveParticipant((prev) => prev ?? incoming.participant ?? null);
    };

    const connect = () => {
      if (cancelled) return;
      setStatus("connecting");
      const next = new EventSource(`${API_URL}/api/messages/stream`);
      source = next;

      next.addEventListener("open", () => setStatus("open"));
      next.addEventListener("snapshot", handleSnapshot);
      next.addEventListener("message", handleMessage);
      next.addEventListener("error", () => {
        if (next.readyState === EventSource.CLOSED) {
          setStatus("closed");
          next.close();
          if (source === next) source = null;
          if (!cancelled) {
            retryTimer = setTimeout(connect, 2000);
          }
        } else {
          setStatus("connecting");
        }
      });
    };

    connect();

    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (source) source.close();
      for (const timer of thinkingTimers.current.values()) clearTimeout(timer);
      thinkingTimers.current.clear();
    };
  }, [upsertMessage]);

  const handleSelect = useCallback((participant: string) => {
    setActiveParticipant(participant);
  }, []);

  const handleClose = useCallback((participant: string) => {
    closedLocally.current.add(participant);
    setConversations((prev) => prev.filter((c) => c.participant !== participant));
    setActiveParticipant((prev) => {
      if (prev !== participant) return prev;
      // pick another conversation if any remain
      return null;
    });
    fetch(`${API_URL}/api/conversations/${encodeURIComponent(participant)}/close`, {
      method: "POST",
    }).catch(() => {
      // best-effort; UI state already updated
    });
  }, []);

  // After closing, fall back to the first remaining conversation if active is null.
  useEffect(() => {
    if (activeParticipant === null && conversations.length > 0) {
      setActiveParticipant(conversations[0].participant);
    }
  }, [activeParticipant, conversations]);

  const isThinkingForActive = useMemo(
    () => (activeParticipant ? thinkingFor.has(activeParticipant) : false),
    [thinkingFor, activeParticipant],
  );

  const liveMentions = useQuery(
    api.mentions.byParticipant,
    activeParticipant ? { participant: activeParticipant, limit: 50 } : "skip",
  );

  const mergedMessages = useMemo(() => {
    if (!liveMentions || liveMentions.length === 0) return messages;
    if (!activeParticipant) return messages;

    const synthesized: Message[] = liveMentions.map((m) => ({
      id: `mention:${m._id}`,
      text: null,
      participant: activeParticipant,
      chatId: null,
      chatKind: "dm",
      service: "agent",
      createdAt: new Date(m.foundAt).toISOString(),
      direction: "outbound",
      mention: {
        platform: m.platform as Platform,
        postUrl: m.postUrl,
        postText: m.postText,
        authorHandle: m.authorHandle,
        postedAt: m.postedAt ?? undefined,
        screenshotUrl: null,
      } satisfies MentionPayload,
    }));

    const existing = new Set(messages.map((m) => m.id));
    return [
      ...messages,
      ...synthesized.filter((s) => !existing.has(s.id)),
    ];
  }, [messages, liveMentions, activeParticipant]);

  return (
    <main className="relative h-screen overflow-hidden bg-[#05030f] font-sans text-zinc-100">
      <BackgroundGlow />

      <div className="relative flex h-full flex-col">
        <header className="flex items-center justify-between border-white/5 border-b bg-black/30 px-6 backdrop-blur-md">
          <div className="flex items-center gap-3 py-3">
            <div className="relative h-2.5 w-2.5">
              <span className="absolute inset-0 rounded-full bg-violet-400 shadow-[0_0_12px_rgba(167,139,250,0.9)]" />
              <span className="absolute inset-0 animate-ping rounded-full bg-violet-400/60" />
            </div>
            <span className="font-medium text-sm tracking-tight">Spectrum</span>
            <span className="text-xs text-zinc-500">/ live console</span>
          </div>
          <div className="flex items-center gap-3 text-[11px]">
            <StatusPill status={status} />
            <AgentPill configured={agentConfigured} />
          </div>
        </header>

        <div className="border-white/5 border-b bg-black/20">
          <Tabs
            conversations={conversations}
            activeParticipant={activeParticipant}
            onSelect={handleSelect}
            onClose={handleClose}
          />
        </div>

        <div className="flex flex-1 overflow-hidden">
          <section className="flex w-[340px] shrink-0 flex-col border-white/5 border-r">
            <ChatThread
              messages={mergedMessages}
              participant={activeParticipant}
              isAgentThinking={isThinkingForActive}
            />
          </section>
          <section className="relative flex-1 bg-[#05030f]">
            <AgentScene />
            <SceneOverlayLive />
          </section>
        </div>
      </div>
    </main>
  );
}

function updateConversations(
  prev: Conversation[],
  m: Message,
): Conversation[] {
  if (!m.participant) return prev;
  const existing = prev.find((c) => c.participant === m.participant);
  if (existing) {
    const isNewer =
      new Date(m.createdAt).getTime() >=
      new Date(existing.lastMessageAt).getTime();
    return prev
      .map((c) =>
        c.participant === m.participant
          ? {
              ...c,
              messageCount: c.messageCount + 1,
              lastMessageAt: isNewer ? m.createdAt : c.lastMessageAt,
              lastMessageText: isNewer ? m.text : c.lastMessageText,
              lastDirection: isNewer ? m.direction : c.lastDirection,
              chatKind: m.chatKind ?? c.chatKind,
            }
          : c,
      )
      .sort(
        (a, b) =>
          new Date(b.lastMessageAt).getTime() -
          new Date(a.lastMessageAt).getTime(),
      );
  }
  return [
    {
      participant: m.participant,
      chatKind: m.chatKind,
      lastMessageAt: m.createdAt,
      lastMessageText: m.text,
      lastDirection: m.direction,
      messageCount: 1,
    },
    ...prev,
  ];
}

function StatusPill({ status }: { status: ConnectionState }) {
  const color =
    status === "open"
      ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.7)]"
      : status === "connecting"
        ? "bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.6)]"
        : "bg-rose-400 shadow-[0_0_8px_rgba(251,113,133,0.6)]";
  return (
    <span className="flex items-center gap-1.5 rounded-full bg-white/[0.04] px-2.5 py-1 font-mono ring-1 ring-white/10">
      <span className={`h-1.5 w-1.5 rounded-full ${color}`} />
      <span className="text-zinc-400 uppercase tracking-wider">{status}</span>
    </span>
  );
}

function AgentPill({ configured }: { configured: boolean | null }) {
  if (configured === null) return null;
  return (
    <span
      className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 font-mono ring-1 ${
        configured
          ? "bg-cyan-500/10 text-cyan-300 ring-cyan-400/20"
          : "bg-zinc-500/10 text-zinc-400 ring-zinc-500/20"
      }`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${
          configured
            ? "bg-cyan-400 shadow-[0_0_8px_rgba(34,211,238,0.7)]"
            : "bg-zinc-500"
        }`}
      />
      <span className="uppercase tracking-wider">
        {configured ? "agent online" : "agent offline"}
      </span>
    </span>
  );
}

function BackgroundGlow() {
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      <div className="-translate-x-1/2 absolute top-0 left-1/2 h-[600px] w-[1200px] rounded-full bg-violet-600/10 blur-[120px]" />
      <div className="absolute right-0 bottom-0 h-[500px] w-[800px] translate-x-1/4 translate-y-1/4 rounded-full bg-cyan-500/10 blur-[100px]" />
    </div>
  );
}

function SceneOverlayLive() {
  const sessions = useQuery(api.sessions.activeCloud) ?? [];
  const liveCount = sessions.length;
  const summary =
    liveCount === 0
      ? "4 browsers · idle"
      : `${liveCount} live · ${4 - Math.min(liveCount, 4)} idle`;
  return (
    <div className="pointer-events-none absolute inset-0 flex flex-col justify-between p-4">
      <div className="flex items-center justify-between">
        <div className="flex flex-col gap-0.5">
          <div className="font-mono text-[10px] text-violet-300/60 uppercase tracking-[0.25em]">
            Browser fleet
          </div>
          <div className="font-medium text-sm text-white/90">Live scrape network</div>
        </div>
        <div
          className={`rounded-full px-2.5 py-1 font-mono text-[10px] ring-1 backdrop-blur ${
            liveCount > 0
              ? "bg-rose-500/10 text-rose-200 ring-rose-400/30"
              : "bg-black/40 text-zinc-400 ring-white/10"
          }`}
        >
          {summary}
        </div>
      </div>
      <div className="flex items-end justify-between font-mono text-[10px] text-zinc-500">
        <div className="flex gap-3">
          <Legend dot="#ff4500" label="Reddit" />
          <Legend dot="#e2e8f0" label="X" />
          <Legend dot="#0a66c2" label="LinkedIn" />
          <Legend dot="#ff0050" label="TikTok" />
        </div>
        <div className="text-zinc-600">drag to rotate</div>
      </div>
    </div>
  );
}

function Legend({ dot, label }: { dot: string; label: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: dot, boxShadow: `0 0 6px ${dot}` }}
      />
      {label}
    </div>
  );
}
