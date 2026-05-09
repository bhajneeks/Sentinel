"use client";

import { useEffect, useMemo, useRef } from "react";
import { maskParticipant, maskedInitial } from "../format";
import type { Message } from "../types";

type Props = {
  messages: Message[];
  participant: string | null;
  isAgentThinking: boolean;
};

const formatTime = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

export default function ChatThread({
  messages,
  participant,
  isAgentThinking,
}: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  const ordered = useMemo(() => {
    if (!participant) return [];
    return messages
      .filter((m) => m.participant === participant)
      .slice()
      .sort(
        (a, b) =>
          new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime(),
      );
  }, [messages, participant]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [ordered.length, isAgentThinking]);

  if (!participant) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
        <div className="text-sm text-zinc-400">No conversation selected</div>
        <div className="text-xs text-zinc-600">
          Pick a tab above, or send an iMessage.
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col bg-[#0a0a0a]">
      <header className="flex flex-col items-center gap-1 border-white/[0.06] border-b px-4 py-3 backdrop-blur">
        <div className="flex h-9 w-9 items-center justify-center rounded-full bg-gradient-to-br from-zinc-700 to-zinc-800 font-medium text-sm text-white ring-1 ring-white/10">
          {maskedInitial(participant)}
        </div>
        <div className="flex items-center gap-1 font-medium text-[13px] text-white/95">
          <span className="tabular-nums">{maskParticipant(participant)}</span>
          <svg
            width="9"
            height="9"
            viewBox="0 0 9 9"
            fill="none"
            className="text-zinc-500"
            aria-hidden="true"
          >
            <title>Conversation</title>
            <path
              d="M2 1L6 4.5L2 8"
              stroke="currentColor"
              strokeWidth="1.2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>
      </header>

      <div className="scrollbar-thin flex-1 overflow-y-auto px-3 py-4">
        <div className="flex flex-col gap-1">
          {ordered.length === 0 && (
            <div className="py-12 text-center text-sm text-zinc-600">
              No messages yet.
            </div>
          )}
          {ordered.map((m, i) => {
            const prev = ordered[i - 1];
            const next = ordered[i + 1];
            const sameAsPrev = prev?.direction === m.direction;
            const sameAsNext = next?.direction === m.direction;
            return (
              <Bubble
                key={m.id}
                message={m}
                groupedTop={sameAsPrev}
                groupedBottom={sameAsNext}
                showTimestamp={!sameAsNext}
              />
            );
          })}
          {isAgentThinking && <ThinkingBubble />}
          <div ref={bottomRef} />
        </div>
      </div>
    </div>
  );
}

function Bubble({
  message,
  groupedTop,
  groupedBottom,
  showTimestamp,
}: {
  message: Message;
  groupedTop: boolean;
  groupedBottom: boolean;
  showTimestamp: boolean;
}) {
  const isOutbound = message.direction === "outbound";

  const radius = isOutbound
    ? `rounded-2xl ${groupedTop ? "rounded-tr-md" : ""} ${groupedBottom ? "rounded-br-md" : ""}`
    : `rounded-2xl ${groupedTop ? "rounded-tl-md" : ""} ${groupedBottom ? "rounded-bl-md" : ""}`;

  const bubbleClass = isOutbound
    ? "bg-gradient-to-b from-[#1F8FFF] to-[#0066CC] text-white shadow-[0_1px_8px_rgba(10,132,255,0.25)]"
    : "bg-[#26252A] text-white";

  return (
    <div
      className={`flex animate-[bubblePop_0.28s_cubic-bezier(0.34,1.56,0.64,1)] flex-col ${
        isOutbound ? "items-end" : "items-start"
      } ${groupedTop ? "mt-0.5" : "mt-2"}`}
    >
      <div
        className={`max-w-[85%] whitespace-pre-wrap break-words px-3.5 py-2 text-[15px] leading-snug ${radius} ${bubbleClass}`}
      >
        {message.text ?? <em className="opacity-60">(no text)</em>}
      </div>
      {showTimestamp && (
        <div
          className={`mt-1 px-1 text-[10px] text-zinc-500 tabular-nums ${
            isOutbound ? "text-right" : "text-left"
          }`}
        >
          {isOutbound ? "Delivered" : ""} {formatTime(message.createdAt)}
        </div>
      )}
    </div>
  );
}

function ThinkingBubble() {
  return (
    <div className="mt-2 flex animate-[bubblePop_0.28s_cubic-bezier(0.34,1.56,0.64,1)] items-end justify-end">
      <div className="flex items-center gap-1 rounded-2xl rounded-br-md bg-gradient-to-b from-[#1F8FFF] to-[#0066CC] px-4 py-3 shadow-[0_1px_8px_rgba(10,132,255,0.25)]">
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
      className="h-1.5 w-1.5 animate-pulse rounded-full bg-white/85"
      style={{ animationDelay: `${delay}s`, animationDuration: "1s" }}
    />
  );
}
