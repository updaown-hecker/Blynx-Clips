import asyncio
import json
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, Request

from .engine import Engine
from .media import run
from .platforms import PlatformError, Source, parse_url

MAX_BYTES = 48 * 1024 * 1024
MAX_SECONDS = 60


async def save_tab_capture(engine: Engine, request: Request, url: str) -> dict:
    received_at = time.time()
    try:
        source = parse_url(url)
    except PlatformError as exc:
        raise HTTPException(422, str(exc)) from None
    if request.headers.get("content-type", "").split(";")[0] != "video/webm":
        raise HTTPException(415, "Expected WebM tab recording")
    if engine.tab_upload.locked():
        raise HTTPException(409, "Another tab recording is being saved")
    channel = engine.current_channel(source.url)
    name = channel["name"] if channel else source.key
    directory = engine.store.clips_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if min(shutil.disk_usage(path).free for path in (directory, engine.store.root)) < 200_000_000:
        raise HTTPException(507, "Insufficient storage for this recording")
    saved = engine.store.item("SELECT coalesce(sum(bytes),0) AS bytes FROM clip WHERE status='ready'")["bytes"]
    if saved + MAX_BYTES > engine.store.settings.disk_gb * 1_000_000_000:
        raise HTTPException(507, "Blynx Clips storage limit reached")

    async with engine.tab_upload:
        with tempfile.TemporaryDirectory(prefix=".tab-", dir=directory) as work:
            scratch = Path(work)
            raw = scratch / "tab.webm"
            size = 0
            try:
                async with asyncio.timeout(35):
                    with raw.open("wb") as file:
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise HTTPException(413, "Tab recording exceeds 48 MB")
                            file.write(chunk)
            except TimeoutError:
                raise HTTPException(408, "Tab recording upload timed out") from None
            return await encode_tab(engine, raw, scratch, directory, source, name, received_at)


async def encode_tab(engine: Engine, raw: Path, scratch: Path, directory: Path,
                     source: Source, name: str, received_at: float) -> dict:
    out = scratch / "clip.mp4"
    cover = scratch / "clip.jpg"
    maximum = min(MAX_SECONDS, engine.store.settings.maximum)
    try:
        async with engine.encode:
            await run("ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                      "-f", "webm", "-i", str(raw), "-t", str(maximum), "-map", "0:v:0", "-map", "0:a:0?",
                      "-vf", "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                      "-threads", "4", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-movflags", "+faststart", str(out), timeout=120)
            probe = json.loads(await run("ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                                         "-of", "json", str(out), timeout=10))
            duration = float(probe["format"]["duration"])
            if duration < 1 or not any(item["codec_type"] == "video" for item in probe["streams"]):
                raise ValueError
            await run("ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-frames:v", "1",
                      "-vf", "scale=480:-2", "-threads", "1", str(cover), timeout=15)
    except (RuntimeError, TimeoutError, ValueError, KeyError):
        raise HTTPException(422, "Chrome did not send a playable recording of at least one second") from None

    key = uuid4().hex
    when = received_at - duration
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:36] or "stream"
    title = f"{prefix}_{datetime.fromtimestamp(when):%Y-%m-%d_%H-%M-%S}_tab_{key[:6]}"
    target = directory / (title + ".mp4")
    size = out.stat().st_size
    try:
        out.replace(target)
        cover.replace(target.with_suffix(".jpg"))
        engine.store.execute("""
            INSERT INTO clip(id,channel,name,platform,broadcast,source,onset,score,offset,
            status,path,reason,duration,bytes,thumbnail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, name, title, source.platform, source.key, source.url, when, 0, 0,
             "ready", str(target), "Recorded manually from a Chrome tab", duration, size, True))
    except Exception:
        target.unlink(missing_ok=True)
        target.with_suffix(".jpg").unlink(missing_ok=True)
        raise
    engine.activity(f"{name} · Chrome tab recording saved")
    if engine.store.settings.notification and shutil.which("notify-send"):
        await engine.notify("Chrome recording saved", f"{duration:.1f} seconds saved")
    return {"id": key, "duration": round(duration, 1)}
