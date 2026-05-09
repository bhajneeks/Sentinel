import { IMessageSDK } from "@photon-ai/imessage-kit";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";
const INGEST_URL = `${BACKEND_URL}/api/messages/ingest`;

type IncomingMessage = {
  id: string;
  text: string | null;
  participant: string | null;
  chatId: string | null;
  chatKind: "dm" | "group" | "unknown";
  service: string | null;
  isFromMe: boolean;
  createdAt: Date;
};

const forward = async (msg: IncomingMessage) => {
  if (msg.isFromMe) return;

  const payload = {
    id: msg.id,
    text: msg.text,
    participant: msg.participant,
    chatId: msg.chatId,
    chatKind: msg.chatKind,
    service: msg.service,
    createdAt: msg.createdAt.toISOString(),
  };

  try {
    const res = await fetch(INGEST_URL, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      console.error(`ingest ${res.status}: ${await res.text()}`);
      return;
    }
    console.log(
      `forwarded: ${msg.participant ?? "?"} (${msg.chatKind}): ${msg.text ?? ""}`
    );
  } catch (err) {
    console.error("ingest failed:", err instanceof Error ? err.message : err);
  }
};

const sdk = new IMessageSDK();

await sdk.startWatching({
  onDirectMessage: forward,
  onGroupMessage: forward,
});

console.log(`bridge watching iMessage → ${INGEST_URL}`);
