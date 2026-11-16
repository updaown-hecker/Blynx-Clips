import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psutil

from .detector import Aggregator, Detection, Detector, Event
from .media import Recorder, assemble, bounds, run
from .platforms import Live, PlatformError, Twitch, YouTube, parse_url, read_secret
from .store import Settings, Store

log = logging.getLogger("blynx")


def public_error(error: BaseException) -> str:
    if isinstance(error, PlatformError):
        return str(error)
    if isinstance(error, httpx.TimeoutException):
        return "Platform request timed out"
    if isinstance(error, httpx.RequestError):
        return "Unable to connect to the platform"
    if isinstance(error, FileNotFoundError):
        return "A required executable or media file is missing"
    return type(error).__name__


@dataclass
class Job:
    event: Event
    id: str
    target: Path
    task: asyncio.Task | None = None
    timing: tuple[float, float, float, float] | None = None


class Engine:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.http = httpx.AsyncClient(timeout=12)
        self.twitch = Twitch(self.http)
        self.youtube = YouTube(self.http, store.quota)
        self.live: dict[str, Live] = {}
        self.status: dict[str, dict] = {}
        self.workers: dict[str, tuple[asyncio.Task, str]] = {}
        self.retry: dict[str, tuple[float, int]] = {}
        self.check_at: dict[str, float] = {}
        self.discovery_due = 0.0
        self.demo = False
        self.running = store.started
        self.stop = False
        self.wake = asyncio.Event()
        self.activities: deque[dict] = deque(maxlen=40)
        self.encode = asyncio.Semaphore(1)
        self.tab_upload = asyncio.Lock()
        self.process = psutil.Process()
        self.process.cpu_percent()
        self.system = {"cpu": 0.0, "memory": 0, "processes": 0, "bytes": 0}
        self.samples: dict[str, deque] = {}
        self.scan: asyncio.Task | None = None
        self.demo_id = "simulation"
        self.auth_task: asyncio.Task | None = None
        self.auth_state = ""
        self.youtube_auth = ""
        self.credential_lock = asyncio.Lock()
        self.manual_clips = {}
        self.notifications: set[asyncio.Task] = set()
        self.buffer = self.store.root / "buffers"
        self.buffer.mkdir(exist_ok=True)

    async def load_credentials(self) -> None:
        client_id = self.store.config("twitch_client_id", "")
        if client_id:
            if client_id != self.twitch.client_id:
                self.twitch.token = ""
            self.twitch.client_id = client_id
        if self.store.config("youtube_saved", False):
            mode = self.store.config("youtube_saved", "")
            value = await read_secret("Blynx Clips YouTube", mode)
            if mode == "api-key":
                self.youtube.api_key = value
            elif mode == "access-token":
                self.youtube.oauth_token = value

    def restart_platform(self, platform: str) -> None:
        for channel in self.store.all("SELECT id FROM channel WHERE platform=?", (platform,)):
            key = channel["id"]
            self.retry.pop(key, None)
            if key in self.workers:
                task, _ = self.workers[key]
                if not task.cancelling():
                    task.cancel()
        self.invalidate()

    def current_channel(self, url: str) -> dict | None:
        source = parse_url(url)
        for channel in self.store.all("SELECT * FROM channel WHERE platform=?", (source.platform,)):
            live = self.live.get(channel["id"])
            if channel["url"] == source.url or (live and live.url == source.url):
                return channel
        return None

    def activity(self, message: str, level: str = "info") -> None:
        self.activities.appendleft({"at": time.time(), "message": message, "level": level})
        log.info("%s: %s", level, message)

    def invalidate(self) -> None:
        self.discovery_due = 0
        self.check_at.clear()
        self.wake.set()

    async def add(self, url: str) -> dict:
        source = parse_url(url)
        adapter = self.twitch if source.platform == "Twitch" else self.youtube
        remote, name, avatar = await adapter.resolve(source)
        video = source.key if source.platform == "YouTube" and source.kind == "video" else ""
        key = uuid4().hex
        self.store.execute("INSERT OR IGNORE INTO channel(id,platform,remote,url,name,avatar,video) VALUES(?,?,?,?,?,?,?)",
                           (key, source.platform, remote, source.url, name, avatar, video))
        self.invalidate()
        return self.store.item("SELECT * FROM channel WHERE platform=? AND remote=? AND video=?",
                               (source.platform, remote, video))

    async def discover(self, channels: list[dict]) -> None:
        now = time.monotonic()
        twitch = [c for c in channels if c["platform"] == "Twitch"]
        if twitch and now >= self.discovery_due:
            self.discovery_due = now + 45
            try:
                live = await self.twitch.live([c["remote"] for c in twitch])
                for c in twitch:
                    self.update_discovery(c, live.get(c["remote"]), "Offline")
            except Exception as exc:
                for c in twitch:
                    self.status.setdefault(c["id"], {})["discovery"] = public_error(exc)
        youtube = [c for c in channels if c["platform"] == "YouTube"]
        search_count = sum(not c["video"] for c in youtube)
        searched = False
        for c in youtube:
            key = c["id"]
            if now < self.check_at.get(key, 0):
                continue
            known = self.live.get(key)
            needs_search = not c["video"] and not known
            if needs_search and searched:
                continue
            searched |= needs_search
            interval = max(60, 86400 * max(1, search_count) / self.store.settings.youtube_search_budget)
            self.check_at[key] = now + (interval if needs_search else 60)
            try:
                live, status = await self.youtube.live(c["remote"], c["video"], known.broadcast if known else "")
                if known and not live and not c["video"]:
                    self.check_at[key] = now + 60
                self.update_discovery(c, live, status)
            except Exception as exc:
                self.status.setdefault(key, {})["discovery"] = public_error(exc)

    def update_discovery(self, channel: dict, live: Live | None, status: str) -> None:
        key = channel["id"]
        data = self.status.setdefault(key, {})
        if live is None:
            self.live.pop(key, None)
            data["discovery"] = status
        else:
            if key not in self.live or self.live[key].broadcast != live.broadcast:
                self.activity(f'{channel["name"]} went live')
            self.live[key] = live
            data["discovery"] = "Live"

    async def recover(self) -> None:
        pending = self.store.all("SELECT * FROM clip WHERE status IN ('capturing','processing')")
        for row in pending:
            data = json.loads(row["journal"])
            try:
                path = Path(data["buffer"])
                if not path.is_relative_to(self.buffer) or not path.is_dir():
                    raise RuntimeError
                recorder = Recorder(path, "", "")
                recorder.anchor = float(data["anchor"])
                recorder.refresh()
                event = Event(row["onset"], row["score"], bool(row["provisional"]),
                              data.get("settled") or time.time(), "recovered after interruption")
                if Path(row["path"]).exists():
                    metadata = json.loads(await run("ffprobe", "-v", "error", "-show_format", "-show_streams",
                                                    "-of", "json", row["path"], timeout=15))
                    duration = float(metadata["format"]["duration"])
                    tracks = {s["codec_type"] for s in metadata["streams"]}
                    if not {"video", "audio"}.issubset(tracks) or not 0 < duration <= data["maximum"]:
                        raise RuntimeError
                    self.ready(row["id"], event, {"duration": duration, "partial": True,
                               "bytes": Path(row["path"]).stat().st_size,
                               "thumbnail": Path(row["path"]).with_suffix(".jpg").exists()})
                else:
                    await self.finalize(recorder, Job(event, row["id"], Path(row["path"])), data)
            except Exception:
                self.store.execute("UPDATE clip SET status='failed',reason='Interrupted before recoverable video was saved' WHERE id=?",
                                   (row["id"],))
        for path in self.buffer.iterdir():
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    def ready(self, key: str, event: Event, metadata: dict) -> None:
        self.store.execute("UPDATE clip SET status='ready',score=?,reason=?,duration=?,bytes=?,partial=?,thumbnail=?,journal='{}' WHERE id=?",
                           (event.peak, event.reason, metadata["duration"], metadata["bytes"], metadata["partial"],
                            metadata["thumbnail"], key))
        self.activity("Highlight ready" + (" · partial video" if metadata["partial"] else ""))
        if self.store.settings.notification and shutil.which("notify-send"):
            task = asyncio.create_task(self.notify("Highlight ready", f'{metadata["duration"]:.1f} seconds saved'))
            self.notifications.add(task)
            task.add_done_callback(self.notifications.discard)

    async def notify(self, title: str, message: str) -> None:
        try:
            await run("notify-send", "--app-name=Blynx Clips", title, message, timeout=5)
        except (OSError, RuntimeError, TimeoutError):
            pass

    async def finalize(self, recorder: Recorder, job: Job, data: dict) -> None:
        self.store.execute("UPDATE clip SET status='processing' WHERE id=?", (job.id,))
        try:
            start, end = bounds(job.event, data["pre"], data["post"], data["maximum"], data["offset"])
            async with self.encode:
                result = await assemble(list(recorder.segments), start, end, job.target,
                                        data["maximum"], data.get("nvenc", False))
            self.ready(job.id, job.event, result)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, RuntimeError) else public_error(exc)
            self.store.execute("UPDATE clip SET status='failed',reason=? WHERE id=?", (reason[-500:], job.id))
            self.activity(f"Clip failed · {reason[-240:]}", "error")

    async def supervisor(self) -> None:
        await self.recover()
        while not self.stop:
            self.wake.clear()
            channels = self.store.all("SELECT * FROM channel WHERE enabled=1 ORDER BY priority DESC, name")
            if self.running and channels and (self.scan is None or self.scan.done()):
                self.scan = asyncio.create_task(self.discover(channels))
            desired: dict[str, tuple[dict, Live]] = {}
            settings = self.store.settings
            disk = shutil.disk_usage(self.store.root)
            output = self.store.clips_dir()
            output.mkdir(parents=True, exist_ok=True)
            output_free = shutil.disk_usage(output).free
            saved = self.store.item("SELECT coalesce(sum(bytes),0) AS bytes FROM clip WHERE status='ready'")["bytes"]
            buffer_bytes = sum(s.get("bufferBytes", 0) for s in self.status.values())
            disk_ok = min(disk.free, output_free) > 150_000_000 and saved + buffer_bytes < settings.disk_gb * 1_000_000_000
            if self.demo:
                demo = {"id": self.demo_id, "name": "Simulation", "platform": "Simulation", "offset": 0,
                        "avatar": "", "preset": ""}
                fake = Live("simulation", "", "Synthetic chat and generated test video")
                desired[self.demo_id] = (demo, fake)
            if self.running and disk_ok:
                for c in channels:
                    key = c["id"]
                    if key in self.live:
                        if len(desired) < settings.slots:
                            desired[key] = (c, self.live[key])
                        else:
                            self.status.setdefault(key, {})["recording"] = "Queued"
            for key, (task, bid) in list(self.workers.items()):
                if task.done():
                    await asyncio.gather(task, return_exceptions=True)
                    del self.workers[key]
                elif not task.cancelling() and (key not in desired or bid != desired[key][1].broadcast):
                    task.cancel()
                    # A worker may be flushing pending footage. Keep the slot reserved.
            for key, (c, live) in desired.items():
                if key not in self.workers and len(self.workers) < settings.slots and (
                    self.retry.get(key, (0, 0))[0] <= time.monotonic()
                ):
                    self.workers[key] = asyncio.create_task(self.record(c, live, settings)), live.broadcast
            processes = self.process.children(recursive=True)
            memory = self.process.memory_info().rss
            for process in processes:
                try:
                    memory += process.memory_info().rss
                except psutil.Error:
                    pass
            self.system = {"cpu": round(self.process.cpu_percent(), 1), "memory": memory, "processes": len(processes),
                           "bytes": saved + buffer_bytes, "diskOk": disk_ok}
            try:
                await asyncio.wait_for(self.wake.wait(), 1)
            except TimeoutError:
                pass

    async def listen(self, adapter, live: Live, message, state) -> None:
        backoff = 2
        while True:
            try:
                await adapter.chat(live, message, state)
                raise PlatformError("Chat connection closed")
            except Exception as exc:
                state(public_error(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def record(self, channel: dict, live: Live, settings: Settings) -> None:
        key = channel["id"]
        mock = key == self.demo_id
        work = uuid4().hex
        recorder = Recorder(self.buffer / work, live.url, settings.quality, mock)
        data = self.status.setdefault(key, {})
        data.pop("error", None)
        data.update({"recording": "Starting recorder", "chat": "Connecting", "discovery": "Live"})
        offset = channel["offset"]
        pre, post, maximum = (6, 6, 35) if mock else (settings.pre, settings.post, settings.maximum)
        preset = channel["preset"] or settings.preset
        rise, spread = {"High": (1.7, 3.5), "Balanced": (2.5, 5), "Low": (3.5, 7),
                        "Manual": (settings.rise, settings.deviations)}[preset]
        detector = Detector(Detection(rise=rise, deviations=spread, baseline=settings.baseline,
                                      min_users=settings.min_users, onset=settings.onset, cooldown=settings.cooldown,
                                      per_hour=settings.per_hour, max_active=maximum - pre - post - 0.5))
        pending: dict[int, Aggregator] = {}
        watermark = 0 if mock else (8 if channel["platform"] == "YouTube" else 2)
        previous_tick = int(time.time()) - watermark
        seen: OrderedDict[str, None] = OrderedDict()
        raw: deque[int] = deque(maxlen=30)
        history: deque = deque(maxlen=150)
        self.samples[key] = history
        jobs: list[Job] = []
        active: Job | None = None
        chat_task = None

        def message(mid: str, author: str, text: str, emote: bool, posted: float | None) -> None:
            if not author or (mid and mid in seen):
                return
            if mid:
                seen[mid] = None
                if len(seen) > 20000:
                    seen.popitem(last=False)
            now = time.time()
            delay = now - posted if posted is not None else None
            if delay is not None and (delay > 12 or delay < -3):
                return
            signature = str(hash(" ".join(text.lower().split())[:160])) if text else ""
            second = int(min(now, posted)) + 1 if posted is not None else int(now) + 1
            if second > previous_tick:
                pending.setdefault(second, Aggregator()).add(author, signature, emote, delay)

        def setchat(value: str) -> None:
            data["chat"] = value

        def journal(job: Job) -> dict:
            pre, post, maximum, offset = job.timing or timing
            return {"buffer": str(recorder.directory), "anchor": recorder.anchor, "settled": job.event.settled,
                    "pre": pre, "post": post, "maximum": maximum, "offset": offset, "nvenc": settings.nvenc}

        timing = pre, post, maximum, offset
        last_manual = 0.0

        def enqueue_manual() -> dict:
            nonlocal last_manual
            now = time.time()
            if now - last_manual < 5:
                raise PlatformError("Wait five seconds between manual clips")
            recorder.refresh()
            if not recorder.segments or recorder.segments[-1].end - recorder.segments[0].start < 5:
                raise PlatformError("The recording buffer is warming up")
            length = min(30.0, maximum - 0.25)
            end = recorder.segments[-1].end
            onset = max(recorder.segments[0].start, end - length)
            clipid = uuid4().hex
            prefix = re.sub(r"[^A-Za-z0-9_-]", "_", channel["name"])[:36] or "stream"
            name = f"{prefix}_{datetime.fromtimestamp(now):%Y-%m-%d_%H-%M-%S}_manual_{clipid[:6]}"
            target = self.store.clips_dir() / (name + ".mp4")
            event = Event(onset, 0, False, settled=end, reason="Saved manually from the rolling buffer")
            job = Job(event, clipid, target, timing=(0, 0, maximum, 0))
            self.store.execute("""
                INSERT INTO clip(id,channel,name,platform,broadcast,source,onset,score,offset,
                status,path,simulated,journal,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (clipid, channel["name"], name, channel["platform"], live.broadcast, live.url, onset,
                 0, 0, "capturing", str(target), mock, json.dumps(journal(job)), event.reason))
            jobs.append(job)
            last_manual = now
            self.activity(f'{channel["name"]} · manual clip requested')
            return {"id": clipid, "duration": round(end - onset, 1)}

        began = time.time()
        self.store.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?)",
                           (work, channel["name"], channel["platform"], began, began, "active", mock))
        outcome = "stopped"
        try:
            await recorder.start()
            self.manual_clips[key] = enqueue_manual
            if mock:
                setchat("Simulation connected")
            else:
                adapter = self.twitch if channel["platform"] == "Twitch" else self.youtube
                chat_task = asyncio.create_task(self.listen(adapter, live, message, setchat))
            demo_index = 0
            while True:
                await asyncio.sleep(1)
                now = time.time()
                recorder.refresh()
                if recorder.consumer.returncode is not None:
                    detail = (recorder.directory / "ffmpeg.log").read_text(errors="replace")[-500:]
                    detail = re.sub(r"https?://[^\s'\"]+", "[stream URL]", detail).strip()
                    raise RuntimeError("Video source ended or FFmpeg failed" + (f": {detail}" if detail else ""))
                if now - (recorder.last_segment or recorder.started) > 40:
                    raise RuntimeError("No completed video segment for 40 seconds")
                when = int(now) - watermark
                if when <= previous_tick:
                    continue
                bucket = pending.pop(when, Aggregator())
                previous_tick = when
                for expired in [second for second in pending if second < when]:
                    pending.pop(expired)
                if mock:
                    for author in range(65 if 32 <= demo_index < 45 else 4):
                        bucket.add(str(author), "synthetic", False, 0)
                    demo_index += 1
                raw.append(bucket.count)
                covered = data["chat"].endswith(" connected")
                result = detector.tick(when, bucket.bucket() if covered else None)
                health = min(settings.buffer, max(0.0, recorder.segments[-1].end - recorder.segments[0].start)) if recorder.segments else 0
                size = sum(item.path.stat().st_size for item in recorder.segments)
                data.update({"recording": "Recording" if recorder.segments else "Buffering",
                             "mps": bucket.count, "raw5": sum(list(raw)[-5:]), "raw15": sum(list(raw)[-15:]),
                             "raw30": sum(raw), "acceleration": raw[-1] - raw[-2] if len(raw) > 1 else 0,
                             "repeats": bucket.repeats, "emotes": bucket.emotes, "health": round(health, 1),
                             "bufferBytes": size, "bitrate": round(size * 8 / health) if health else 0,
                             "ingestLag": round(bucket.delay_total / bucket.delay_samples, 2) if bucket.delay_samples else None,
                             **detector.metrics})
                sample = {"at": when, "messages": data["raw5"], "baseline": data.get("baseline", 0),
                          "active": detector.active is not None}
                if result and result[0] == "start":
                    event = result[1]
                    clipid = uuid4().hex
                    name = "{}_{}_spike-{}_{}".format(
                        re.sub(r"[^A-Za-z0-9_-]", "_", channel["name"])[:36] or "stream",
                        datetime.fromtimestamp(event.onset).strftime("%Y-%m-%d_%H-%M-%S"),
                        round(event.peak), clipid[:6])
                    target = self.store.clips_dir() / (name + ".mp4")
                    active = Job(event, clipid, target)
                    self.store.execute("""
                        INSERT OR IGNORE INTO clip(id,channel,name,platform,broadcast,source,onset,score,offset,
                        status,path,provisional,simulated,journal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (clipid, channel["name"], name, channel["platform"], live.broadcast, live.url, event.onset,
                         event.peak, offset, "capturing", str(target), event.provisional, mock, json.dumps(journal(active))))
                    self.activity(f'{channel["name"]} · chat spike detected')
                    sample["event"] = clipid
                elif result and result[0] == "end" and active:
                    self.store.execute("UPDATE clip SET journal=?,score=? WHERE id=?",
                                       (json.dumps(journal(active)), active.event.peak, active.id))
                    jobs.append(active)
                    active = None
                for job in jobs:
                    _, end = bounds(job.event, *(job.timing or timing))
                    if job.task is None and recorder.segments and recorder.segments[-1].end >= end:
                        job.task = asyncio.create_task(self.finalize(recorder, job, journal(job)))
                jobs = [job for job in jobs if not job.task or not job.task.done()]
                pins = [bounds(job.event, *(job.timing or timing))[0] for job in jobs + ([active] if active else [])]
                recorder.prune(min([now - settings.buffer, *pins]))
                history.append(sample)
                self.store.execute("UPDATE session SET ended=? WHERE id=?", (now, work))
                if mock and demo_index >= 65 and not jobs and active is None:
                    self.demo = False
                    outcome = "complete"
                    break
        except asyncio.CancelledError:
            self.activity(f'{channel["name"]} · recording stopped')
        except Exception as exc:
            outcome = "failed"
            message_text = str(exc) if isinstance(exc, RuntimeError) else public_error(exc)
            count = self.retry.get(key, (0, 0))[1] + 1
            self.retry[key] = (time.monotonic() + min(120, 2 ** min(7, count)), count)
            data["error"] = message_text
            self.activity(f'{channel["name"]} · {message_text}', "error")
            if mock:
                self.demo = False
        finally:
            self.manual_clips.pop(key, None)
            if chat_task:
                chat_task.cancel()
                await asyncio.gather(chat_task, return_exceptions=True)
            await recorder.stop()
            if active:
                active.event.settled = time.time()
                active.event.reason = "video recording interrupted"
                jobs.append(active)
            for job in jobs:
                if job.task is None:
                    self.store.execute("UPDATE clip SET journal=? WHERE id=?", (json.dumps(journal(job)), job.id))
                    job.task = asyncio.create_task(self.finalize(recorder, job, journal(job)))
            if jobs:
                await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)
            self.store.stop_recent_session(work, outcome)
            data.update({"recording": "Retrying" if outcome == "failed" else "Idle",
                         "chat": "Disconnected", "health": 0, "bufferBytes": 0})
            shutil.rmtree(recorder.directory, ignore_errors=True)
            self.wake.set()

    def snapshot(self) -> dict:
        channels = self.store.all("SELECT * FROM channel ORDER BY priority DESC, name")
        if self.demo or self.demo_id in self.workers:
            channels.insert(0, {"id": "simulation", "name": "Simulation", "platform": "Simulation",
                                "enabled": 1, "priority": 100, "offset": 0, "avatar": "", "url": "", "preset": ""})
        for c in channels:
            key = c["id"]
            c.update(self.status.get(key, {}))
            if not c["enabled"]:
                c["discovery"] = "Disabled"
            if key not in self.live and c.get("recording") == "Queued":
                c["recording"] = "Idle"
            if key in self.live:
                c["live"] = asdict(self.live[key])
            if key == self.demo_id:
                c["live"] = {"title": "Synthetic chat and generated test video"}
            c["samples"] = list(self.samples.get(key, []))
        clips = self.store.all("""SELECT id,channel,name,platform,broadcast,source,onset,score,offset,reason,status,
                                review,favorite,duration,bytes,partial,thumbnail,provisional,simulated
                                FROM clip ORDER BY onset DESC LIMIT 500""")
        return {"channels": channels, "clips": clips, "activities": list(self.activities), "running": self.running,
                "demo": self.demo, "settings": self.store.settings.model_dump(), "system": self.system,
                "integration": {"twitchConfigured": bool(self.twitch.client_id), "twitchAuth": self.auth_state,
                                "twitchClientId": self.twitch.client_id, "youtubeAuth": self.youtube_auth,
                                "youtubeConfigured": bool(self.youtube.api_key or self.youtube.oauth_token)}}

    async def shutdown(self) -> None:
        self.stop = True
        self.wake.set()
        if self.scan:
            self.scan.cancel()
            await asyncio.gather(self.scan, return_exceptions=True)
        if self.auth_task:
            self.auth_task.cancel()
            await asyncio.gather(self.auth_task, return_exceptions=True)
        for task, _ in self.workers.values():
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*(task for task, _ in self.workers.values()), return_exceptions=True)
        if self.notifications:
            await asyncio.gather(*self.notifications, return_exceptions=True)
        await self.http.aclose()

