"""Analysis and rendering for the Cut Clips editor."""

import asyncio
import hashlib
import json
import logging
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .captions import write_ass
from .media import run, transcribe_audio

Layout = Literal["fill", "fit", "camera-top", "camera-bottom"]
CaptionStyle = Literal["off", "clean", "pop", "boxed"]
Progress = Callable[[str, dict | None], None]
logger = logging.getLogger("blynx")


class Camera(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(ge=0.04, le=1)
    height: float = Field(ge=0.04, le=1)

    @model_validator(mode="after")
    def within_frame(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("Camera selection must fit within the source frame")
        return self


class Cue(BaseModel):
    start: float = Field(ge=0, le=360)
    end: float = Field(ge=0, le=360)
    text: str = Field(max_length=180)


class ShortEdit(BaseModel):
    start: float = Field(0, ge=0, le=300)
    end: float = Field(ge=1, le=300)
    layout: Layout = "fill"
    camera: Camera | None = None
    caption_style: CaptionStyle = "clean"
    cues: list[Cue] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def valid_edit(self):
        if self.end - self.start < 1:
            raise ValueError("Select at least one second of video")
        if self.layout.startswith("camera-") and self.camera is None:
            raise ValueError("Select the camera area before exporting this layout")
        return self


class EditorJobs:
    def __init__(self):
        self.states: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.worker = asyncio.Lock()

    def submit(self, operation: Callable[[Progress], Awaitable[dict]]) -> str:
        if len(self.tasks) >= 8:
            raise ValueError("The editor queue is full. Try again after a job finishes.")
        if len(self.states) >= 64:
            oldest = next(key for key in self.states if key not in self.tasks)
            del self.states[oldest]
        key = secrets.token_hex(16)
        state = self.states[key] = {"status": "queued", "message": "Waiting for the editor", "result": None}

        def progress(message: str, result: dict | None = None) -> None:
            state.update(status="running", message=message)
            if result is not None:
                state["result"] = result

        async def work() -> None:
            try:
                async with self.worker:
                    progress("Preparing video")
                    result = await operation(progress)
                    state.update(status="done", message="Finished", result=result)
            except Exception as exc:
                logger.exception("Editor job failed")
                message = str(exc) if isinstance(exc, ValueError) else "Video processing failed. Try again."
                state.update(status="error", message=message)
            finally:
                self.tasks.pop(key, None)

        self.tasks[key] = asyncio.create_task(work())
        return key

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def stable_camera(samples: list[list[tuple[float, float, float, float]]]) -> dict | None:
    groups: list[dict[int, tuple[float, float, float, float]]] = []
    for frame, faces in enumerate(samples):
        for face in faces:
            x, y, width, height = face
            if width > 0.26 or height > 0.38 or not (x < 0.34 or x > 0.66 or y < 0.25 or y > 0.75):
                continue
            for group in groups:
                anchor = next(iter(group.values()))
                if abs(x - anchor[0]) < 0.075 and abs(y - anchor[1]) < 0.085:
                    group.setdefault(frame, face)
                    break
            else:
                groups.append({frame: face})
    def confidence(group):
        x = median(face[0] for face in group.values())
        return len(group), abs(x - 0.5)

    best = max(groups, key=confidence, default={})
    if len(best) < max(2, (len(samples) + 3) // 4):
        return None
    x, y, face_width, face_height = map(median, zip(*best.values()))
    width = min(0.56, max(0.16, face_width * 2.7))
    height = min(0.72, max(0.16, face_height * 2.25))
    return {"x": min(1 - width, max(0, x - width / 2)),
            "y": min(1 - height, max(0, y - height * 0.42)), "width": width, "height": height}


def detect_frames(frames: bytes, width: int, height: int) -> dict | None:
    import cv2
    import numpy as np

    cv2.setNumThreads(1)
    cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    if cascade.empty():
        raise RuntimeError("OpenCV face detector files are missing")
    frame_size = width * height
    samples = []
    for offset in range(0, len(frames) - frame_size + 1, frame_size):
        frame = np.frombuffer(frames, dtype=np.uint8, count=frame_size, offset=offset).reshape(height, width)
        try:
            faces = cascade.detectMultiScale(frame, 1.12, 5, minSize=(17, 17))
        except cv2.error as exc:
            raise RuntimeError("Could not scan video frame") from exc
        samples.append([(float(x + w / 2) / width, float(y + h / 2) / height,
                         float(w) / width, float(h) / height) for x, y, w, h in faces])
    return stable_camera(samples)


async def analyze(source: Path, duration: float, root: Path, progress: Progress) -> dict:
    stat = source.stat()
    fingerprint = hashlib.sha256(f"v1:{source}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
    cache = root / "editor-analysis" / f"{fingerprint}.json"
    try:
        return json.loads(cache.read_text())
    except (OSError, ValueError):
        pass
    info = json.loads(await run("ffprobe", "-v", "error", "-show_entries",
                                "stream=codec_type,width,height", "-of", "json", str(source)))
    streams = info["streams"]
    video = next((stream for stream in streams if stream["codec_type"] == "video"), None)
    if video is None:
        raise ValueError("This file has no video track.")
    result = {"camera": None, "cues": [], "camera_error": "", "caption_error": ""}
    progress("Looking for the streamer’s camera", None)
    try:
        scale = min(1, 640 / max(video["width"], video["height"]))
        width, height = (max(2, int(video[key] * scale) // 2 * 2) for key in ("width", "height"))
        frames = await run("ffmpeg", "-v", "error", "-i", str(source), "-an", "-filter_threads", "1",
                           "-vf", f"fps={14 / max(1, duration):.6f},scale={width}:{height},format=gray",
                           "-frames:v", "14", "-threads", "1", "-f", "rawvideo", "pipe:1")
        result["camera"] = await asyncio.to_thread(detect_frames, frames, width, height)
    except (ImportError, OSError, RuntimeError, TimeoutError, ValueError):
        logger.exception("Camera detection failed")
        result["camera_error"] = "Auto detection unavailable. Draw the camera area manually."

    progress("Generating captions from audio", result.copy())
    if any(stream["codec_type"] == "audio" for stream in streams):
        try:
            with tempfile.TemporaryDirectory(prefix="blynx-editor-audio-") as work:
                audio = Path(work) / "audio.wav"
                await run("ffmpeg", "-v", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(audio))
                transcript = await transcribe_audio(audio, root / "whisper", short_cues=True)
                result["cues"] = [{"start": max(0, round(start, 3)), "end": min(300, round(end, 3)),
                                   "text": label[:180]} for start, end, label in transcript
                                  if end > start and start < 300 and label.strip()][:2000]
        except (OSError, RuntimeError, TimeoutError, ValueError):
            logger.exception("Caption generation failed")
            result["caption_error"] = "Could not generate captions. Retry analysis or export with captions off."
    if not (result["camera_error"] or result["caption_error"]):
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix(".tmp")
            temporary.write_text(json.dumps(result), encoding="utf-8")
            temporary.replace(cache)
        except OSError:
            logger.warning("Could not cache editor analysis", exc_info=True)
    return result


def cover(width: int, height: int) -> str:
    return (f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop={width}:{height},setsar=1")


def layout_filter(layout: Layout, camera: Camera | None) -> str:
    if layout == "fit":
        return ("[0:v:0]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if camera is None or layout == "fill":
        return "[0:v:0]" + cover(1080, 1920)
    crop = (f"crop=trunc(iw*{camera.width:.8f}/2)*2:trunc(ih*{camera.height:.8f}/2)*2:"
            f"iw*{camera.x:.8f}:ih*{camera.y:.8f}")
    order = "[camera][game]" if layout == "camera-top" else "[game][camera]"
    return (f"[0:v:0]split[face][desktop];[face]{crop},{cover(1080, 640)}[camera];"
            f"[desktop]{cover(1080, 1280)}[game];{order}vstack=inputs=2")


async def render(source: Path, target: Path, edit: ShortEdit, end: float, credit: str = "") -> None:
    duration = end - edit.start
    graph = layout_filter(edit.layout, edit.camera)
    with tempfile.TemporaryDirectory(prefix="blynx-editor-render-") as work:
        path = Path(work)
        cues = [(cue.start - edit.start, cue.end - edit.start, cue.text) for cue in edit.cues
                if edit.caption_style != "off"]
        if credit or cues:
            margin = 520 if edit.layout == "camera-bottom" else 136
            style = "clean" if edit.caption_style == "off" else edit.caption_style
            write_ass(path / "captions.ass", credit, duration, cues, style=style, margin=margin)
            graph += ",ass=captions.ass"
        await run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(edit.start),
                  "-i", str(source.absolute()), "-t", str(duration), "-filter_complex_threads", "2",
                  "-filter_complex", graph + "[picture]", "-map", "[picture]", "-map", "0:a:0?",
                  "-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-movflags", "+faststart", str(target.absolute()), cwd=path, timeout=600)
