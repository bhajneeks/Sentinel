"use client";

import { maskParticipant } from "../format";
import type { Conversation } from "../types";

type Props = {
  conversations: Conversation[];
  activeParticipant: string | null;
  onSelect: (participant: string) => void;
  onClose: (participant: string) => void;
};

const truncate = (s: string, n = 18) => (s.length > n ? `${s.slice(0, n)}…` : s);

export default function Tabs({
  conversations,
  activeParticipant,
  onSelect,
  onClose,
}: Props) {
  if (conversations.length === 0) {
    return (
      <div className="flex h-12 items-center px-6 text-xs text-zinc-500">
        No active conversations yet — incoming messages will appear here.
      </div>
    );
  }

  return (
    <div className="scrollbar-thin flex h-12 items-stretch gap-1 overflow-x-auto px-3">
      {conversations.map((conv) => {
        const isActive = conv.participant === activeParticipant;
        return (
          <div
            key={conv.participant}
            className={`group relative flex shrink-0 items-center gap-2 rounded-t-md px-3 text-sm transition-colors ${
              isActive
                ? "bg-white/[0.06] text-white"
                : "text-zinc-400 hover:bg-white/[0.03] hover:text-zinc-200"
            }`}
          >
            <button
              type="button"
              onClick={() => onSelect(conv.participant)}
              className="flex items-center gap-2 py-2"
            >
              <span
                className={`h-1.5 w-1.5 rounded-full transition-colors ${
                  isActive
                    ? "bg-violet-400 shadow-[0_0_8px_rgba(167,139,250,0.8)]"
                    : conv.lastDirection === "inbound"
                      ? "bg-emerald-400/70"
                      : "bg-zinc-600"
                }`}
              />
              <span className="font-medium tabular-nums">
                {truncate(maskParticipant(conv.participant))}
              </span>
              <span className="text-xs text-zinc-500">{conv.messageCount}</span>
            </button>
            <button
              type="button"
              aria-label={`Close ${maskParticipant(conv.participant)}`}
              onClick={() => onClose(conv.participant)}
              className="flex h-5 w-5 items-center justify-center rounded text-zinc-500 opacity-0 transition-opacity hover:bg-white/10 hover:text-zinc-200 group-hover:opacity-100 data-[active=true]:opacity-100"
              data-active={isActive}
            >
              <svg
                width="10"
                height="10"
                viewBox="0 0 10 10"
                fill="none"
                aria-hidden="true"
              >
                <title>Close tab</title>
                <path
                  d="M1 1L9 9M9 1L1 9"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
              </svg>
            </button>
            {isActive && (
              <span className="-bottom-px absolute inset-x-0 h-px bg-gradient-to-r from-transparent via-violet-400 to-transparent" />
            )}
          </div>
        );
      })}
    </div>
  );
}
