import asyncio
import os
import shutil
import time
from pathlib import Path

import httpx
import pytest

from blynx.detector import Aggregator, Bucket, Detection, Detector, Event
from blynx.engine import Engine
from blynx.media import Recorder, Segment, assemble, bounds
from blynx.platforms import Live, PlatformError, Twitch, YouTube, parse_url
from blynx.store import Store


@pytest.mark.parametrize(("url", "platform", "kind"), [
    ("https://m.twitch.tv/SomeChannel", "Twitch", "login"),
    ("https://www.twitch.tv/videos/123456", "Twitch", "video"),
    ("https://www.youtube.com/@Streamer/live", "YouTube", "handle"),
    ("https://youtu.be/aqz-KE-bpKQ?t=3", "YouTube", "video"),
    ("https://www.youtube.com/live/aqz-KE-bpKQ", "YouTube", "video"),
])
def test_source(url, platform, kind):
    source = parse_url(url)
    assert (source.platform, source.kind) == (platform, kind)


@pytest.mark.parametrize("url", ["file:///tmp/evil", "http://twitch.tv:81/name", "https://twitch.tv@elsewhere.tv/channel",
                                 "https://youtube.com.invalid/@someone", "https://www.twitch.tv/directory"])
def test_bad_source(url):
    with pytest.raises(PlatformError):
        parse_url(url)


def scenarios(low, event, seconds=180, single=False):
    detector = Detector(Detection())
    events = []
    for when in range(seconds):
        count = event(when) if when >= 30 else low
        authors = {str(i) for i in range(min(count, 20))} if not single or when < 30 else {"spam"}
        result = detector.tick(when, Bucket(min(count, 3) if single and when >= 30 else count, authors))
        if result:
            events.append(result)
    return events


@pytest.mark.parametrize(("base", "burst"), [(2, 18), (40, 180), (400, 1800)])
def test_relative_burst(base, burst):
    events = scenarios(base, lambda t: burst if 42 <= t < 72 else base)
    assert [kind for kind, _ in events] == ["start", "end"]
    assert events[1][1].reason == "activity settled"


def test_baseline_spam_and_coverage():
    aggregator = Aggregator()
    for _ in range(100):
        aggregator.add("flooder", "same", True, 2)
    assert aggregator.count == 100 and aggregator.repeats == 99 and aggregator.bucket().count == 3
    assert not scenarios(400, lambda _: 400)
    assert not scenarios(4, lambda _: 90, single=True)
    detector = Detector(Detection())
    authors = {str(i) for i in range(12)}
    for t in range(30):
        detector.tick(t, Bucket(40, authors))
    assert detector.metrics["baseline"] == 200
    for t in range(30, 36):
        assert not detector.tick(t, None)
    assert not detector.tick(36, Bucket(900, authors))


def test_merge_and_cooldown():
    events = scenarios(4, lambda t: 32 if 42 <= t < 58 or 61 <= t < 74 or 93 <= t < 98 else 4)
    assert len(events) == 2 and events[-1][1].settled > 74


def test_sustained_and_bounds():
    events = scenarios(40, lambda t: 180 if t >= 45 else 40, seconds=400)
    assert len(events) == 2 and events[-1][1].reason == "duration cap"
    start, end = bounds(Event(100, 18, False, 210), 20, 20, 120, -6)
    assert start == 74
    assert end - start < 120
    assert bounds(Event(100, 18, False, 110), 20, 20, 120, 6) == (86, 136)


def test_retention(tmp_path):
    recorder = Recorder(tmp_path, "", "best")
    for i in range(3):
        path = tmp_path / f"segment-{i:08}.ts"
        path.touch()
        recorder.segments.append(Segment(path, i*4, (i+1)*4))
    recorder.prune(7)
    assert [s.end for s in recorder.segments] == [8, 12]
    assert not (tmp_path / "segment-00000000.ts").exists()


def test_platform_responses(tmp_path, monkeypatch):
    monkeypatch.setenv("BLYNX_TWITCH_CLIENT_ID", "local-test")
    monkeypatch.setenv("BLYNX_TWITCH_TOKEN", "invalid")
    monkeypatch.setenv("BLYNX_YOUTUBE_API_KEY", "local-test")
    async def test():
        def handler(req):
            if "id.twitch.tv" in req.url.host:
                return httpx.Response(401, json={})
            if req.url.path.endswith("/videos"):
                return httpx.Response(200, json={"items": [{
                    "snippet": {"title": "Fixture", "liveBroadcastContent": "live"},
                    "liveStreamingDetails": {"actualStartTime": "2026-01-01T00:00:00Z", "activeLiveChatId": "chat"}
                }]})
            raise httpx.ConnectError("transport failed", request=req)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            twitch = Twitch(client)
            with pytest.raises(PlatformError, match="401"):
                await twitch.validate()
            youtube = YouTube(client, Store(tmp_path).quota)
            live, status = await youtube.live("remote", "aqz-KE-bpKQ")
            assert status == "Live" and live.chat == "chat"
            with pytest.raises(httpx.ConnectError):
                await youtube.live("remote")
    asyncio.run(test())


def test_three_slots_and_release(tmp_path):
    async def test():
        store = Store(tmp_path)
        engine = Engine(store)
        engine.running = True
        captured = set()
        for index in range(4):
            key = str(index)
            platform = "Twitch" if index % 2 else "YouTube"
            store.execute("INSERT INTO channel(id,platform,remote,url,name,avatar,video,priority) VALUES(?,?,?,?,?,?,?,?)",
                          (key, platform, key, "url", key, "", "", 100-index))
            engine.live[key] = Live(key, "", "Test")
        async def discover(_channels):
            return
        async def record(channel, _live, _settings):
            captured.add(channel["id"])
            try:
                await asyncio.Event().wait()
            finally:
                captured.remove(channel["id"])
        engine.discover = discover
        engine.record = record
        supervisor = asyncio.create_task(engine.supervisor())
        try:
            await asyncio.sleep(.1)
            assert captured == {"0", "1", "2"}
            assert engine.status["3"]["recording"] == "Queued"
            engine.live.pop("0")
            engine.wake.set()
            await asyncio.sleep(1.3)
            assert captured == {"1", "2", "3"}
        finally:
            await engine.shutdown()
            await supervisor
    asyncio.run(test())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_real_media_in_progress(tmp_path):
    async def test():
        recorder = Recorder(tmp_path / "buffer", "", "best", simulated=True)
        await recorder.start()
        try:
            async with asyncio.timeout(18):
                while len(recorder.segments) < 2:
                    await asyncio.sleep(.25)
                    recorder.refresh()
                    assert recorder.consumer.returncode is None, (recorder.directory / "ffmpeg.log").read_text()
            first = recorder.segments[0].start
            target = tmp_path / "highlight.mp4"
            result = await assemble(recorder.segments, first + 0.5, first + 5, target, 5)
            assert target.is_file() and result["thumbnail"] and 3 < result["duration"] <= 5
            assert recorder.consumer.returncode is None
            await recorder.stop()
            store = Store(tmp_path / "app")
            engine = Engine(store)
            moved = engine.buffer / "previous-run"
            recorder.directory.rename(moved)
            import json
            data = {"buffer": str(moved), "anchor": recorder.anchor, "settled": first+4,
                    "pre": 0, "post": 1, "maximum": 6, "offset": 0}
            recovered = tmp_path / "recovered.mp4"
            store.execute("""INSERT INTO clip(id,channel,name,platform,broadcast,source,onset,score,offset,status,path,journal)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                          ("id", "fixture", "recovered", "Simulation", "sim", "", first+1, 20, 0, "capturing",
                           str(recovered), json.dumps(data)))
            await engine.recover()
            assert recovered.is_file()
            assert store.item("SELECT status FROM clip WHERE id='id'")["status"] == "ready"
            await engine.shutdown()
        finally:
            await recorder.stop()
    asyncio.run(test())



@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("streamlink"),
                    reason="FFmpeg and Streamlink required")
def test_streamlink_pipe(tmp_path):
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from blynx.media import run

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    async def test():
        public = tmp_path / "fixture"
        public.mkdir()
        await run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=192x108:rate=15",
                  "-f", "lavfi", "-i", "sine=sample_rate=44100", "-t", "16", "-c:v", "libx264",
                  "-threads", "1", "-preset", "ultrafast", "-g", "30", "-c:a", "aac",
                  "-f", "hls", "-hls_time", "2", "-hls_list_size", "0", str(public / "video.m3u8"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=public))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        recorder = Recorder(tmp_path / "buffer", f"hls://http://127.0.0.1:{server.server_port}/video.m3u8", "best")
        try:
            await recorder.start()
            async with asyncio.timeout(20):
                await recorder.consumer.wait()
            await recorder.stop()
            assert recorder.producer is not None and recorder.producer.returncode == 0
            assert recorder.consumer.returncode == 0, (recorder.directory / "ffmpeg.log").read_text()
            assert len(recorder.segments) >= 3
            start = recorder.segments[0].start
            result = await assemble(recorder.segments, start, start + 4, tmp_path / "fixture.mp4", 5)
            assert 3.5 <= result["duration"] <= 5
        finally:
            await recorder.stop()
            server.shutdown()
            server.server_close()
            thread.join(2)

    asyncio.run(test())
