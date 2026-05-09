"""Send iMessages via macOS Messages.app using AppleScript."""

from __future__ import annotations

import asyncio
import logging
import sys

logger = logging.getLogger("uvicorn.error")


def _escape_applescript(text: str) -> str:
    out = text.replace("\\", "\\\\").replace('"', '\\"')
    return out.replace("\n", "\\n").replace("\r", "\\r")


async def send(participant: str, text: str, service: str | None = None) -> bool:
    """Send `text` to `participant` via Messages.app. Returns True on success."""
    if sys.platform != "darwin":
        logger.info("imessage send skipped: not on macOS")
        return False
    if not participant or not text:
        return False

    service_token = "SMS" if (service or "").lower() == "sms" else "iMessage"
    safe_text = _escape_applescript(text)
    safe_participant = _escape_applescript(participant)

    script = (
        'tell application "Messages"\n'
        f"    set targetService to 1st service whose service type = {service_token}\n"
        f'    set targetBuddy to buddy "{safe_participant}" of targetService\n'
        f'    send "{safe_text}" to targetBuddy\n'
        "end tell\n"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(script.encode("utf-8"))
        if proc.returncode != 0:
            logger.warning(
                "imessage send failed (%s): %s",
                proc.returncode,
                stderr.decode("utf-8", errors="replace").strip(),
            )
            return False
        return True
    except Exception as exc:
        logger.warning("imessage send error: %s", exc)
        return False
