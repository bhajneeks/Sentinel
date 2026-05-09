"""Overshoot vision client - watch a promo video and analyze it.

Overshoot is a realtime streaming API: you create a "stream" session, publish
video into the LiveKit room it gives you, then query an OpenAI-compatible
/chat/completions endpoint that can address any moment in the stream via a
special ovs:// URI.  See https://overshoot.mintlify.app/intro.

Workflow here:
  1. POST /v1/streams          -> stream_id + LiveKit room url + token
  2. Decode the source video   -> PyAV (handles local files and URLs)
  3. Publish frames into the   -> livekit.rtc VideoSource at PUBLISH_FPS,
     LiveKit room                 downscaled to TARGET_HEIGHT_PX to control cost
  4. POST /v1/chat/completions -> two prompts: HOOK (first ~3s) and LAYOUT
                                  (full duration), referenced by absolute
                                  stream timestamps for determinism
  5. DELETE /v1/streams/{id}   -> release the lease

Required env var:
    OVERSHOOT_API_KEY  - 'ovs-...' bearer token from platform.overshoot.ai
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import av
import httpx
from livekit import rtc

OVERSHOOT_BASE_URL = "https://api.overshoot.ai/v1"
DEFAULT_MODEL = "google/gemma-4-E4B-it"
KEEPALIVE_INTERVAL_S = 60.0
PUBLISH_FPS = 5
TARGET_HEIGHT_PX = 480

HOOK_PROMPT = (
    "You are a short-form video strategist analyzing the HOOK (the opening "
    "~3 seconds) of a TikTok-style promo video. In 4-6 sentences, cover:\n"
    "- The opening visual (subject, framing, motion, lighting).\n"
    "- Any on-screen text or spoken hook line you can read.\n"
    "- The emotional or curiosity trigger the creator is reaching for.\n"
    "- Why this hook would (or wouldn't) make a scroller stop, and one "
    "concrete tweak that would make it stronger."
)

LAYOUT_PROMPT = (
    "Break this promo video down into its structural sections (e.g. hook, "
    "problem, product reveal, demo, social proof, objection handling, CTA). "
    "For each section give:\n"
    "- A short label.\n"
    "- The approximate timestamp range (mm:ss-mm:ss).\n"
    "- 1-2 sentences on what happens and the persuasion job it's doing.\n"
    "End with one sentence on overall pacing and one on the call to action."
)


class OvershootConfigError(RuntimeError):
    pass


class OvershootAPIError(RuntimeError):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"Overshoot API returned {status}: {body}")
        self.status = status
        self.body = body


def _headers() -> dict[str, str]:
    key = os.environ.get("OVERSHOOT_API_KEY")
    if not key:
        raise OvershootConfigError(
            "OVERSHOOT_API_KEY must be set (get one at platform.overshoot.ai)."
        )
    return {"Authorization": f"Bearer {key}"}


async def _post(
    client: httpx.AsyncClient, path: str, json: dict | None = None,
) -> dict[str, Any]:
    resp = await client.post(path, headers=_headers(), json=json or {})
    if resp.status_code >= 400:
        try:
            body: Any = resp.json()
        except Exception:
            body = resp.text
        raise OvershootAPIError(resp.status_code, body)
    return resp.json() if resp.content else {}


async def _delete(client: httpx.AsyncClient, path: str) -> None:
    resp = await client.delete(path, headers=_headers())
    if resp.status_code >= 400 and resp.status_code != 404:
        try:
            body: Any = resp.json()
        except Exception:
            body = resp.text
        raise OvershootAPIError(resp.status_code, body)


async def create_stream(client: httpx.AsyncClient) -> dict[str, Any]:
    return await _post(client, "/streams")


async def keepalive(client: httpx.AsyncClient, stream_id: str) -> dict[str, Any]:
    return await _post(client, f"/streams/{stream_id}/keepalive")


async def delete_stream(client: httpx.AsyncClient, stream_id: str) -> None:
    await _delete(client, f"/streams/{stream_id}")


async def chat_completion(
    client: httpx.AsyncClient, model: str, messages: list[dict[str, Any]],
) -> dict[str, Any]:
    return await _post(
        client, "/chat/completions",
        json={"model": model, "messages": messages},
    )


def _publish_dims(src_w: int, src_h: int, max_h: int) -> tuple[int, int]:
    if src_h <= max_h:
        w, h = src_w, src_h
    else:
        h = max_h
        w = int(src_w * (max_h / src_h))
    # H.264-style encoders prefer even dimensions; LiveKit is happier too.
    return w - (w % 2), h - (h % 2)


def _frame_iter(container: "av.container.InputContainer"):
    v_stream = container.streams.video[0]
    for packet in container.demux(v_stream):
        for frame in packet.decode():
            yield frame


async def _publish_video(
    *,
    room_url: str,
    token: str,
    source_path: str,
    target_fps: int,
    max_height: int,
) -> dict[str, Any]:
    """Decode `source_path` with PyAV and publish into the LiveKit room."""
    container = await asyncio.to_thread(av.open, source_path)
    try:
        v_stream = container.streams.video[0]
        v_stream.thread_type = "AUTO"
        src_w = v_stream.codec_context.width
        src_h = v_stream.codec_context.height
        out_w, out_h = _publish_dims(src_w, src_h, max_height)

        room = rtc.Room()
        await room.connect(room_url, token)
        try:
            source = rtc.VideoSource(out_w, out_h)
            track = rtc.LocalVideoTrack.create_video_track("promo", source)
            opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA)
            await room.local_participant.publish_track(track, opts)

            frame_interval = 1.0 / float(target_fps)
            decoder = _frame_iter(container)
            frames = 0
            loop = asyncio.get_event_loop()
            t0 = loop.time()

            while True:
                av_frame = await asyncio.to_thread(next, decoder, None)
                if av_frame is None:
                    break
                arr = await asyncio.to_thread(
                    _to_rgba, av_frame, out_w, out_h,
                )
                vframe = rtc.VideoFrame(
                    out_w, out_h, rtc.VideoBufferType.RGBA, arr.tobytes(),
                )
                source.capture_frame(vframe)
                frames += 1

                target = t0 + frames * frame_interval
                now = loop.time()
                if target > now:
                    await asyncio.sleep(target - now)

            # Give Overshoot a beat to ingest the tail of the stream.
            await asyncio.sleep(0.5)
            duration_s = loop.time() - t0
            return {
                "source_width": src_w,
                "source_height": src_h,
                "publish_width": out_w,
                "publish_height": out_h,
                "publish_fps": target_fps,
                "duration_s": round(duration_s, 2),
                "frames_published": frames,
            }
        finally:
            await room.disconnect()
    finally:
        container.close()


def _to_rgba(frame: "av.VideoFrame", width: int, height: int):
    return frame.reformat(width=width, height=height, format="rgba").to_ndarray()


async def _keepalive_loop(
    client: httpx.AsyncClient, stream_id: str, stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=KEEPALIVE_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await keepalive(client, stream_id)
        except OvershootAPIError:
            return  # stream gone; nothing left to renew


def _extract_text(resp: dict[str, Any]) -> str:
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""


async def analyze_promo_video(
    source: str,
    *,
    model: str = DEFAULT_MODEL,
    hook_window_ms: int = 3000,
    publish_fps: int = PUBLISH_FPS,
    max_height: int = TARGET_HEIGHT_PX,
) -> dict[str, Any]:
    """Stream `source` through Overshoot, return hook + layout analysis.

    `source` is anything PyAV / ffmpeg can open: a local path, http(s) mp4,
    HLS playlist, etc.  TikTok page URLs won't work directly - resolve to the
    underlying mp4 first (e.g. with yt-dlp).
    """
    _headers()  # fail fast if the env var is missing

    async with httpx.AsyncClient(
        base_url=OVERSHOOT_BASE_URL, timeout=120.0,
    ) as client:
        stream = await create_stream(client)
        stream_id = stream["id"]
        publish = stream.get("publish") or {}
        room_url = publish.get("url")
        token = publish.get("token")
        if not room_url or not token:
            await delete_stream(client, stream_id)
            raise OvershootAPIError(
                500, f"stream response missing publish info: {stream}",
            )

        stop = asyncio.Event()
        keep_task = asyncio.create_task(
            _keepalive_loop(client, stream_id, stop),
        )

        try:
            video_meta = await _publish_video(
                room_url=room_url,
                token=token,
                source_path=source,
                target_fps=publish_fps,
                max_height=max_height,
            )

            duration_ms = max(int(video_meta["duration_s"] * 1000), 1)
            hook_end_ms = min(hook_window_ms, duration_ms)

            # Absolute stream timestamps - first frame is t=0, last is duration_ms.
            hook_uri = (
                f"ovs://streams/{stream_id}"
                f"?start_timestamp_ms=0&end_timestamp_ms={hook_end_ms}"
            )
            full_uri = (
                f"ovs://streams/{stream_id}"
                f"?start_timestamp_ms=0&end_timestamp_ms={duration_ms}"
            )

            def msg(prompt: str, uri: str) -> list[dict[str, Any]]:
                return [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "video_url", "video_url": {"url": uri}},
                    ],
                }]

            hook_resp, layout_resp = await asyncio.gather(
                chat_completion(client, model, msg(HOOK_PROMPT, hook_uri)),
                chat_completion(client, model, msg(LAYOUT_PROMPT, full_uri)),
            )
        finally:
            stop.set()
            await keep_task
            try:
                await delete_stream(client, stream_id)
            except OvershootAPIError:
                pass  # best-effort cleanup

    return {
        "stream_id": stream_id,
        "model": model,
        "video": {"source": source, **video_meta},
        "hook": {
            "window_ms": hook_end_ms,
            "uri": hook_uri,
            "analysis": _extract_text(hook_resp),
            "raw": hook_resp,
        },
        "layout": {
            "uri": full_uri,
            "analysis": _extract_text(layout_resp),
            "raw": layout_resp,
        },
    }
