import asyncio
import csv
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from .detector import Event


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    end: float


def bounds(event: Event, pre: float, post: float, maximum: float, offset: float) -> tuple[float, float]:
    start = event.onset + offset - pre
    end = (event.settled if event.settled is not None else event.onset) + offset + post
    return start, min(start + maximum - 0.25, end)


async def run(*args: str, timeout: float = 180) -> bytes:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(f"{args[0]} failed ({proc.returncode}): {err.decode(errors='replace')[-500:]}")
    return out


async def halt(proc: asyncio.subprocess.Process | None, grace: float = 4) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
        await asyncio.wait_for(proc.wait(), grace)
    except ProcessLookupError:
        pass
    except TimeoutError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


class Recorder:
    def __init__(self, directory: Path, url: str, quality: str, simulated: bool = False) -> None:
        self.directory = directory
        self.url = url
        self.quality = quality
        self.simulated = simulated
        self.producer: asyncio.subprocess.Process | None = None
        self.consumer: asyncio.subprocess.Process | None = None
        self.segments: list[Segment] = []
        self.anchor: float | None = None
        self.index_pos = 0
        self.last_segment = 0.0
        self.started = time.time()
        self.log = None

    async def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=False)
        self.log = (self.directory / "ffmpeg.log").open("wb")
        base = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error"]
        output = ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                  "-f", "segment", "-segment_time", "4", "-segment_format", "mpegts",
                  "-segment_list", "segments.csv", "-segment_list_type", "csv",
                  "segment-%08d.ts"]
        if self.simulated:
            command = base + ["-re", "-f", "lavfi", "-i", "testsrc2=size=480x270:rate=24",
                              "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
            output[3] = "1:a:0"
            output[4:6] = ["-c:v", "libx264", "-preset", "ultrafast", "-threads", "2",
                           "-g", "48", "-c:a", "aac"]
            self.consumer = await asyncio.create_subprocess_exec(
                *command, *output, cwd=self.directory, stderr=self.log,
                stdout=asyncio.subprocess.DEVNULL)
            return
        read_fd, write_fd = os.pipe()
        try:
            self.producer = await asyncio.create_subprocess_exec(
                "streamlink", "--stdout", "--loglevel", "error", "--stream-segment-timeout", "15",
                self.url, self.quality, stdout=write_fd, stderr=self.log)
            self.consumer = await asyncio.create_subprocess_exec(
                *base, "-i", "pipe:0", *output, cwd=self.directory, stdin=read_fd,
                stderr=self.log, stdout=asyncio.subprocess.DEVNULL)
        except BaseException:
            await halt(self.producer)
            raise
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def refresh(self) -> None:
        index = self.directory / "segments.csv"
        if not index.exists():
            return
        rows = []
        with index.open("rb") as file:
            file.seek(self.index_pos)
            while line := file.readline():
                if not line.endswith(b"\n"):
                    break
                self.index_pos = file.tell()
                rows.append(next(csv.reader([line.decode("utf-8")])))
        if not rows:
            return
        now = time.time()
        if self.anchor is None:
            self.anchor = now - float(rows[-1][2])
        self.last_segment = now
        for name, raw_start, raw_end in rows:
            if Path(name).name == name and name.startswith("segment-"):
                self.segments.append(Segment(self.directory / name, self.anchor + float(raw_start),
                                             self.anchor + float(raw_end)))

    def prune(self, cutoff: float) -> None:
        kept = []
        for segment in self.segments:
            if segment.end < cutoff:
                segment.path.unlink(missing_ok=True)
            else:
                kept.append(segment)
        self.segments = kept

    async def stop(self) -> None:
        await halt(self.producer)
        if self.producer is not None and self.consumer is not None and self.consumer.returncode is None:
            try:
                await asyncio.wait_for(self.consumer.wait(), 2)
            except TimeoutError:
                pass
        await halt(self.consumer)
        if self.log is not None:
            self.log.close()
        self.refresh()


async def assemble(segments: list[Segment], start: float, end: float, target: Path,
                   maximum: float, nvenc: bool = False) -> dict:
    selected = [item for item in segments if item.end > start and item.start < end]
    if not selected:
        raise RuntimeError("No completed video overlaps this event")
    earliest = max(start, selected[0].start)
    latest = min(end, selected[-1].end)
    if latest - earliest < 1:
        raise RuntimeError("Less than one second of video overlaps this event")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".partial.mp4")
    manifest = selected[0].path.parent / f"{target.stem}.concat"
    manifest.write_text("".join(f"file '{item.path.name}'\n" for item in selected))
    try:
        arguments = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "1",
                     "-i", str(manifest), "-ss", f"{earliest - selected[0].start:.3f}",
                     "-t", f"{min(latest - earliest, maximum - 0.25):.3f}",
                     "-map", "0:v:0", "-map", "0:a:0", "-pix_fmt", "yuv420p"]
        encoding = ["-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-crf", "20"]
        output = ["-c:a", "aac", "-movflags", "+faststart", str(temporary)]
        if nvenc:
            try:
                await run(*arguments, "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", *output)
            except RuntimeError:
                await run(*arguments, *encoding, *output)
        else:
            await run(*arguments, *encoding, *output)
        info = json.loads(await run("ffprobe", "-v", "error", "-show_streams", "-show_format",
                                    "-of", "json", str(temporary), timeout=15))
        duration = float(info["format"]["duration"])
        if not {"video", "audio"}.issubset({track["codec_type"] for track in info["streams"]}):
            raise RuntimeError("Clip verification failed: audio or video is missing")
        if not 0 < duration <= maximum:
            raise RuntimeError("Clip verification failed: duration exceeds configured maximum")
        temporary.replace(target)
        thumbnail = target.with_suffix(".jpg")
        try:
            await run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(min(2, duration / 2)),
                      "-i", str(target), "-vf", "scale=640:-2", "-frames:v", "1", str(thumbnail), timeout=15)
        except RuntimeError:
            thumbnail.unlink(missing_ok=True)
        return {"duration": duration, "bytes": target.stat().st_size,
                "partial": earliest - start > 1 or end - latest > 1, "thumbnail": thumbnail.exists()}
    finally:
        manifest.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)

