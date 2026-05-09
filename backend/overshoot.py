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
import logging
import os
import sys
from typing import Any

import av
import httpx
from livekit import rtc

log = logging.getLogger("overshoot")

OVERSHOOT_BASE_URL = "https://api.overshoot.ai/v1"
DEFAULT_MODEL = "google/gemma-4-31B-it"
KEEPALIVE_INTERVAL_S = 60.0
PUBLISH_FPS = 2  # Gemma's video budget is ~60 frames; keep us well under
TARGET_HEIGHT_PX = 480
PLAYBACK_SPEED = 4.0  # 1.0 = realtime, higher = faster publish but compressed stream-clock
PUBLISH_MIN_INTERVAL_S = 0.02  # cap at ~50 fps wire to avoid livekit backpressure
POST_PUBLISH_SETTLE_S = 1.5    # let Overshoot finish ingesting before querying

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
    """All decoded frames, in order."""
    v_stream = container.streams.video[0]
    for packet in container.demux(v_stream):
        for frame in packet.decode():
            yield frame


def _sampled_frames(container: "av.container.InputContainer", target_fps: float):
    """Yield (frame, source_time_s) sampled to ~target_fps based on source PTS.

    This is what makes us fast: a 30s video at 30fps source decodes ~900 frames
    but we only forward 60 of them (at 2 fps), so we can publish in seconds
    instead of minutes.
    """
    v_stream = container.streams.video[0]
    time_base = float(v_stream.time_base) if v_stream.time_base else None
    interval_s = 1.0 / float(target_fps)
    next_t = 0.0
    fallback_idx = 0
    for frame in _frame_iter(container):
        if frame.pts is not None and time_base is not None:
            t = frame.pts * time_base
        else:
            t = fallback_idx / 30.0  # rough fallback if PTS missing
        fallback_idx += 1
        if t + 1e-6 >= next_t:
            yield frame, t
            next_t = t + interval_s


class _Publisher:
    """Owns the LiveKit room + video source for the lifetime of an analysis.

    We do NOT disconnect after publishing - Overshoot ends the stream as soon
    as the publisher leaves, which 404s subsequent /chat/completions calls.
    Use as an async context manager.
    """

    def __init__(self, room_url: str, token: str) -> None:
        self._room_url = room_url
        self._token = token
        self.room: rtc.Room | None = None

    async def __aenter__(self) -> "_Publisher":
        self.room = rtc.Room()
        await self.room.connect(self._room_url, self._token)
        log.info("livekit: connected to room")
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self.room is not None:
            await self.room.disconnect()
            log.info("livekit: disconnected")


async def _publish_video(
    publisher: _Publisher,
    *,
    source_path: str,
    target_fps: int,
    max_height: int,
    playback_speed: float,
) -> dict[str, Any]:
    """Decode `source_path` with PyAV and publish into the already-connected room.

    Frames are paced at source_pts/playback_speed wall-clock seconds, so a
    37s source video at playback_speed=4 publishes in ~9s while preserving
    proportional stream timestamps - the chat completion can address e.g.
    'first 3 source seconds' as `end_timestamp_ms = 3000/playback_speed`.
    """
    assert publisher.room is not None, "publisher must be entered first"
    container = await asyncio.to_thread(av.open, source_path)
    try:
        v_stream = container.streams.video[0]
        v_stream.thread_type = "AUTO"
        src_w = v_stream.codec_context.width
        src_h = v_stream.codec_context.height
        out_w, out_h = _publish_dims(src_w, src_h, max_height)

        source_duration_s = 0.0
        if v_stream.duration is not None and v_stream.time_base is not None:
            source_duration_s = float(v_stream.duration * v_stream.time_base)
        elif container.duration:
            source_duration_s = container.duration / 1_000_000.0

        log.info(
            "publish: source=%dx%d (%.1fs) -> output=%dx%d at %d fps src, %.1fx speed",
            src_w, src_h, source_duration_s, out_w, out_h, target_fps, playback_speed,
        )

        source = rtc.VideoSource(out_w, out_h)
        track = rtc.LocalVideoTrack.create_video_track("promo", source)
        opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA)
        await publisher.room.local_participant.publish_track(track, opts)
        log.info("publish: track published")

        decoder = _sampled_frames(container, target_fps)
        frames = 0
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        last_publish = 0.0

        while True:
            picked = await asyncio.to_thread(next, decoder, None)
            if picked is None:
                break
            av_frame, src_t = picked
            arr = await asyncio.to_thread(_to_rgba, av_frame, out_w, out_h)
            vframe = rtc.VideoFrame(
                out_w, out_h, rtc.VideoBufferType.RGBA, arr.tobytes(),
            )

            # Pace by source PTS / playback_speed so stream-clock is proportional
            # to source-clock. Min interval also keeps livekit from choking.
            target_wall = t0 + (src_t / playback_speed)
            now = loop.time()
            sleep_for = max(target_wall - now, PUBLISH_MIN_INTERVAL_S - (now - last_publish))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

            source.capture_frame(vframe)
            last_publish = loop.time()
            frames += 1
            if frames == 1 or frames % 25 == 0:
                log.info("publish: %d frames sent", frames)

        # Let Overshoot finish ingesting the tail before any query.
        await asyncio.sleep(POST_PUBLISH_SETTLE_S)
        wall_duration_s = loop.time() - t0
        log.info(
            "publish: done, %d frames, %.2fs wall, %.2fs source",
            frames, wall_duration_s, source_duration_s,
        )
        return {
            "source_width": src_w,
            "source_height": src_h,
            "publish_width": out_w,
            "publish_height": out_h,
            "publish_fps": target_fps,
            "playback_speed": playback_speed,
            "source_duration_s": round(source_duration_s, 2),
            "wall_duration_s": round(wall_duration_s, 2),
            "frames_published": frames,
        }
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


def _extract_text(resp: dict[str, Any] | None) -> str:
    if not resp:
        return ""
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""


def _ok_or_log(label: str, result: Any) -> dict[str, Any] | None:
    if isinstance(result, BaseException):
        log.warning("chat: %s completion failed: %s", label, result)
        return None
    return result


def _result_payload(label: str, uri: str, resp: dict[str, Any] | None) -> dict[str, Any]:
    base = {"uri": uri, "analysis": _extract_text(resp), "raw": resp}
    if resp is None:
        base["error"] = f"{label} completion failed - see server logs"
    return base


async def analyze_promo_video(
    source: str,
    *,
    model: str = DEFAULT_MODEL,
    hook_window_ms: int = 3000,
    publish_fps: int = PUBLISH_FPS,
    max_height: int = TARGET_HEIGHT_PX,
    playback_speed: float = PLAYBACK_SPEED,
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
            async with _Publisher(room_url, token) as publisher:
                video_meta = await _publish_video(
                    publisher,
                    source_path=source,
                    target_fps=publish_fps,
                    max_height=max_height,
                    playback_speed=playback_speed,
                )

                # Stream-clock is wall-clock since first frame; we paced source
                # PTS by /playback_speed, so source ms maps to stream ms by the
                # same factor.
                stream_duration_ms = max(
                    int(video_meta["wall_duration_s"] * 1000), 1,
                )
                src_to_stream = 1.0 / playback_speed
                hook_end_stream_ms = min(
                    int(hook_window_ms * src_to_stream), stream_duration_ms,
                )
                hook_end_stream_ms = max(hook_end_stream_ms, 1)

                # Hook uses individual frame anchors - very short video_url
                # windows trip an upstream tensor bug. Spread 4 frames across
                # the hook window.
                hook_frame_count = 4
                hook_frame_ts = [
                    int(i * hook_end_stream_ms / max(hook_frame_count - 1, 1))
                    for i in range(hook_frame_count)
                ]
                hook_frame_uris = [
                    f"ovs://streams/{stream_id}?timestamp_ms={ts}"
                    for ts in hook_frame_ts
                ]
                full_uri = (
                    f"ovs://streams/{stream_id}"
                    f"?start_timestamp_ms=0&end_timestamp_ms={stream_duration_ms}"
                )

                src_dur_s = video_meta["source_duration_s"]
                hook_window_s = hook_window_ms / 1000.0
                context_note = (
                    f"\n\nContext: you are seeing frames sampled from a "
                    f"{src_dur_s:.1f}-second source video. Report timestamps "
                    "in source-video time (mm:ss), not stream time."
                )
                hook_context = (
                    f"\n\nContext: these {hook_frame_count} frames are evenly "
                    f"spaced across the first ~{hook_window_s:.1f} seconds of a "
                    f"{src_dur_s:.1f}-second promo video."
                )

                hook_content: list[dict[str, Any]] = [
                    {"type": "text", "text": HOOK_PROMPT + hook_context},
                ]
                for uri in hook_frame_uris:
                    hook_content.append(
                        {"type": "image_url", "image_url": {"url": uri}},
                    )
                hook_messages = [{"role": "user", "content": hook_content}]
                layout_messages = [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": LAYOUT_PROMPT + context_note},
                        {"type": "video_url", "video_url": {"url": full_uri}},
                    ],
                }]

                log.info("chat: requesting hook + layout analysis")
                hook_res, layout_res = await asyncio.gather(
                    chat_completion(client, model, hook_messages),
                    chat_completion(client, model, layout_messages),
                    return_exceptions=True,
                )
                hook_resp = _ok_or_log("hook", hook_res)
                layout_resp = _ok_or_log("layout", layout_res)
                log.info("chat: both completions returned")
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
            "source_window_ms": hook_window_ms,
            "stream_window_ms": hook_end_stream_ms,
            "frame_uris": hook_frame_uris,
            "analysis": _extract_text(hook_resp),
            "raw": hook_resp,
            **({"error": "hook completion failed - see server logs"}
               if hook_resp is None else {}),
        },
        "layout": _result_payload("layout", full_uri, layout_resp),
    }


async def list_models() -> dict[str, Any]:
    """Sanity-check: GET /v1/models needs no auth and returns served models."""
    async with httpx.AsyncClient(
        base_url=OVERSHOOT_BASE_URL, timeout=15.0,
    ) as client:
        resp = await client.get("/models")
        resp.raise_for_status()
        return resp.json()


def _main() -> None:
    """CLI: `python overshoot.py path-or-url [--model ...] [--hook-window-ms ...]`"""
    import argparse
    import json
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Analyze a promo video through Overshoot.",
    )
    parser.add_argument(
        "source", nargs="?",
        help="Local path or URL ffmpeg can open (mp4/HLS/...).",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--hook-window-ms", type=int, default=3000)
    parser.add_argument("--publish-fps", type=int, default=PUBLISH_FPS)
    parser.add_argument("--max-height", type=int, default=TARGET_HEIGHT_PX)
    parser.add_argument(
        "--playback-speed", type=float, default=PLAYBACK_SPEED,
        help="1.0 = realtime publish; higher publishes faster but compresses stream-clock.",
    )
    parser.add_argument(
        "--list-models", action="store_true",
        help="Just hit GET /v1/models and exit (good API-key smoke test).",
    )
    args = parser.parse_args()

    if args.list_models:
        print(json.dumps(asyncio.run(list_models()), indent=2))
        return

    if not args.source:
        parser.error("source is required unless --list-models is set")

    try:
        result = asyncio.run(analyze_promo_video(
            args.source,
            model=args.model,
            hook_window_ms=args.hook_window_ms,
            publish_fps=args.publish_fps,
            max_height=args.max_height,
            playback_speed=args.playback_speed,
        ))
    except (OvershootConfigError, OvershootAPIError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    _main()
