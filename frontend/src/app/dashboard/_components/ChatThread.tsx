"use client";

import { useEffect, useRef } from "react";
import type { Message } from "../types";

type Props = {
  messages: Message[];
  participant: string | null;
  isAgentThinking: boolean;
};

const formatTime = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

const initial = (s: string | null) => {
  if (!s) return "?";
  const cleaned = s.replace(/[^a-z0-9]/gi, "");
  return (cleaned[0] ?? s[0] ?? "?").toUpperCase();
};

export default function ChatThread({
  messages,
  participant,
  isAgentThinking,
}: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, isAgentThinking]);

  if (!participant) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center">
        <div className="text-zinc-400 text-sm">No conversation selected</div>
        <div className="text-xs text-zinc-600">
          Pick a tab above, or send an iMessage to your Mac.
        </div>
      </div>
    );
  }

  const filtered = messages.filter((m) => m.participant === participant);

  return (
    <div className="flex h-full flex-col">
      <div className="border-white/5 border-b px-6 py-4">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-full bg-gradient-to-br from-violet-500/30 to-cyan-500/30 font-semibold text-sm text-white ring-1 ring-white/10">
            {initial(participant)}
          </div>
          <div className="flex flex-col">
            <div className="font-medium text-sm text-white">{participant}</div>
            <div className="text-xs text-zinc-500">
              {filtered.length} message{filtered.length === 1 ? "" : "s"}
            </div>
          </div>
        </div>
      </div>

      <div className="scrollbar-thin flex-1 overflow-y-auto px-6 py-4">
        <div className="flex flex-col gap-3">
          {filtered.length === 0 && (
            <div className="py-12 text-center text-sm text-zinc-500">
              No messages in this thread yet.
            </div>
          )}
          {filtered.map((m) => (
            <MessageBubble key={m.id} message={m} />
          ))}
          {isAgentThinking && <ThinkingBubble />}
          <div ref={bottomRef} />
        </div>
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: Message }) {
  const isOutbound = message.direction === "outbound";

  return (
    <div
      className={`flex animate-[fadeInUp_0.25s_ease-out] gap-2 ${
        isOutbound ? "flex-row-reverse" : "flex-row"
      }`}
    >
      <div
        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs ring-1 ${
          isOutbound
            ? "bg-gradient-to-br from-cyan-500/40 to-violet-500/40 text-cyan-100 ring-cyan-400/30"
            : "bg-white/[0.04] text-zinc-300 ring-white/10"
        }`}
      >
        {isOutbound ? "AI" : initial(message.participant)}
      </div>
      <div
        className={`flex max-w-[75%] flex-col gap-1 ${
          isOutbound ? "items-end" : "items-start"
        }`}
      >
        <div
          className={`whitespace-pre-wrap break-words rounded-2xl px-4 py-2.5 text-sm shadow-lg backdrop-blur ${
            isOutbound
              ? "rounded-br-md bg-gradient-to-br from-cyan-500/20 to-violet-500/20 text-cyan-50 ring-1 ring-cyan-400/20"
              : "rounded-bl-md bg-white/[0.04] text-zinc-100 ring-1 ring-white/10"
          }`}
        >
          {message.text ?? <em className="text-zinc-500">(no text)</em>}
        </div>
        <div className="px-1 text-[10px] text-zinc-600 tabular-nums">
          {formatTime(message.createdAt)}
        </div>
      </div>
    </div>
  );
}

function ThinkingBubble() {
  return (
    <div className="flex animate-[fadeInUp_0.25s_ease-out] flex-row-reverse gap-2">
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-cyan-500/40 to-violet-500/40 text-cyan-100 text-xs ring-1 ring-cyan-400/30">
        AI
      </div>
      <div className="flex items-center gap-1 rounded-2xl rounded-br-md bg-gradient-to-br from-cyan-500/20 to-violet-500/20 px-4 py-3 ring-1 ring-cyan-400/20">
        <Dot delay={0} />
        <Dot delay={0.15} />
        <Dot delay={0.3} />
      </div>
    </div>
  );
}

function Dot({ delay }: { delay: number }) {
  return (
    <span
      className="h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-300"
      style={{ animationDelay: `${delay}s`, animationDuration: "1s" }}
    />
  );
}
