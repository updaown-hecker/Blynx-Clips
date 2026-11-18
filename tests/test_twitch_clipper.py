import asyncio
import json
import shutil

import pytest

from blynx.anonymous_twitch import AnonymousTwitch
from blynx.captions import write_ass
from blynx.detector import Bucket
from blynx.engine import Engine
from blynx.media import Segment, assemble, bounds, run
from blynx.spikes import WindowDetector
from blynx.store import Store


def test_exact_three_times_mean_does_not_trigger_and_spike_cuts_thirty_seconds():
    def events(levels):
        detector = WindowDetector()
        results = []
        for second, count in enumerate(n for level in levels for n in [level] * 10):
            result = detector.tick(second, Bucket(count, set()))
            if result:
                results.append(result)
        return results

    assert events([1, 1, 1, 3]) == []
    [(started, event), (finished, settled)] = events([1, 1, 1, 4, 1])
    assert (started, finished) == ("start", "end") and event is settled
    assert event.onset == 34 and 0 < event.peak <= 100
    assert bounds(event, 15, 15, 30.25, 0) == (19, 49)


def test_disconnected_chat_is_not_counted_as_silence():
    detector = WindowDetector()
    for sec in range(34):
        detector.tick(sec, Bucket(1, set()))
    detector.tick(34, None)
    assert not detector.metrics
    for sec in range(35, 44):
        assert detector.tick(sec, Bucket(6, set())) is None
    assert detector.metrics == {}
    assert detector.tick(44, Bucket(6, set()))[0] == "start"
    assert detector.metrics["raw10"] == 60


def test_anonymous_irc_counts_only_chat_and_replies_to_ping():
    class Socket:
        def __init__(self):
            self.sent = []

        async def send(self, line):
            self.sent.append(line)

    async def scenario():
        ws = Socket()
        chat = AnonymousTwitch("sample_channel")
        messages = []
        frame = (
            "PING :tmi.twitch.tv\r\n"
            ":tmi.twitch.tv 366 justinfan123 #sample_channel :End of names\r\n"
            "@id=m1;tmi-sent-ts=1000000;user-id=42 :someone!someone@host PRIVMSG #sample_channel :hi!\r\n"
            ":another!another@host PRIVMSG #another_channel :ignore"
        )
        assert await chat.receive(ws, frame, "justinfan123", lambda *message: messages.append(message))
        assert messages == [("m1", "42", "hi!", False, 1000.0)]
        assert ws.sent == ["PONG :tmi.twitch.tv"]

    asyncio.run(scenario())


def test_no_token_discovery_and_existing_clips_survive(tmp_path, monkeypatch):
    monkeypatch.setenv("BLYNX_TWITCH_CLIP_CHANNEL", "Allowed_Demo")
    store = Store(tmp_path)
    old_clip = tmp_path / "existing.mp4"
    old_clip.write_bytes(b"historic video")
    store.execute("""INSERT INTO clip(id,channel,platform,broadcast,name,source,onset,score,offset,path,status)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  ("kept", "other", "Twitch", "old", "Saved highlight", "", 1, 0, 0, str(old_clip), "ready"))

    async def fake_streamlink(*args, **_kwargs):
        assert args[0] == "streamlink"
        return b'{"metadata":{"id":"abc","title":"Live test"},"streams":{"best":{}}}'

    monkeypatch.setattr("blynx.anonymous_twitch.run", fake_streamlink)

    async def scenario():
        engine = Engine(store)
        try:
            row = await engine.add("https://www.twitch.tv/allowed_demo")
            await engine.discover([row])
            assert engine.live[row["id"]].broadcast == "abc"
            assert engine.snapshot()["clips"][0]["id"] == "kept"
        finally:
            await engine.shutdown()

    asyncio.run(scenario())
    assert old_clip.read_bytes() == b"historic video"
    assert store.item("SELECT status FROM clip WHERE id='kept'")["status"] == "ready"
    store.db.close()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_credited_vertical_clip_processes_transcript_and_renders_mp4(tmp_path, monkeypatch):
    import blynx.media as media

    caption_file = tmp_path / "check.ass"
    write_ass(caption_file, "allowed_demo", 30, [(0.5, 2.3, "{unsafe} Hello")])
    assert "Twitch: allowed_demo" in caption_file.read_text()
    assert "Caption,,0,0,0,,unsafe Hello" in caption_file.read_text()

    async def fake_whisper(*args, **kwargs):
        if args[1:3] == ("-m", "blynx.captions"):
            return b'[[0.25,1.5,"This is a caption"]]'
        return await run(*args, **kwargs)

    monkeypatch.setattr(media, "run", fake_whisper)

    async def scenario():
        segment = tmp_path / "segment-00000000.ts"
        target = tmp_path / "vertical.mp4"
        await run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12",
                  "-f", "lavfi", "-i", "sine=sample_rate=48000", "-t", "3", "-c:v", "libx264",
                  "-threads", "2", "-c:a", "aac", "-f", "mpegts", str(segment))
        result = await assemble([Segment(segment, 0, 3)], 0.5, 3, target, 30.25,
                                credit="allowed_demo", model_cache=tmp_path)
        assert result["duration"] > 2 and not result["caption_failed"]
        data = json.loads(await run("ffprobe", "-v", "error", "-show_streams", "-of", "json", str(target)))
        video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
        assert (video["width"], video["height"]) == (720, 1280)
        assert {stream["codec_type"] for stream in data["streams"]} == {"video", "audio"}

    asyncio.run(scenario())
