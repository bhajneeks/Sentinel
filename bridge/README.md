# bridge

Tiny Bun watcher that tails iMessage via [`@photon-ai/imessage-kit`](https://github.com/photon-hq/imessage-kit) and POSTs each incoming message to the FastAPI backend's `/api/messages/ingest` endpoint.

## Prerequisites

- macOS
- Messages.app signed in and running (the SDK uses AppleScript)
- **Full Disk Access** for the terminal running this script (System Settings → Privacy & Security → Full Disk Access). The SDK reads `~/Library/Messages/chat.db`.
- Bun ≥ 1.0 (Node ≥ 20 also works if you swap `bun` for `node --experimental-strip-types`)

## Run

```sh
bun install
bun run start
```

Override the backend URL if the FastAPI server isn't on localhost:8000:

```sh
BACKEND_URL=http://otherhost:8000 bun run start
```

Outgoing messages (`isFromMe: true`) are filtered out — the dashboard only shows what people are texting **to** the Mac.
