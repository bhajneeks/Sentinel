export type Direction = "inbound" | "outbound";
export type ChatKind = "dm" | "group" | "unknown";
export type Platform = "reddit" | "x" | "linkedin" | "tiktok";

export type MentionPayload = {
  platform: Platform;
  postUrl: string;
  postText: string;
  authorHandle: string;
  postedAt?: number;
  screenshotUrl?: string | null;
};

export type Message = {
  id: string;
  text: string | null;
  participant: string | null;
  chatId: string | null;
  chatKind: ChatKind;
  service: string | null;
  createdAt: string;
  direction: Direction;
  mention?: MentionPayload;
};

export type Conversation = {
  participant: string;
  chatKind: ChatKind;
  lastMessageAt: string;
  lastMessageText: string | null;
  lastDirection: Direction;
  messageCount: number;
};

export type ConnectionState = "connecting" | "open" | "closed";
