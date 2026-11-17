import asyncio

import pytest

from blynx.detector import Bucket, Detection, Detector
from blynx.engine import Engine
from blynx.platforms import PlatformError, read_secret, write_secret
from blynx.store import Store


def test_server_credentials_roundtrip(tmp_path, monkeypatch):
    directory = tmp_path / "credentials"
    monkeypatch.setenv("BLYNX_CREDENTIAL_DIR", str(directory))

    async def exercise():
        await write_secret("Twitch", "example", "first")
        await write_secret("Twitch", "example", "refreshed")
        assert await read_secret("Twitch", "example") == "refreshed"
        assert await read_secret("Twitch", "another") == ""

    asyncio.run(exercise())
    assert directory.stat().st_mode & 0o777 == 0o700
    files = list(directory.iterdir())
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600


def test_server_follows_one_channel_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("BLYNX_SERVER_MODE", "1")
    monkeypatch.setenv("BLYNX_FOLLOW_URL", "https://twitch.tv/Target")

    async def exercise():
        store = Store(tmp_path)
        assert store.settings.slots == 1
        assert not store.settings.notification
        engine = Engine(store)

        async def resolve(source):
            assert source.key == "target"
            return "123", "Target", ""

        engine.twitch.resolve = resolve
        await engine.register_follow_channel()
        await engine.register_follow_channel()
        with pytest.raises(PlatformError, match="server follows"):
            await engine.add("https://twitch.tv/another")
        await engine.shutdown()
        store.db.close()

        restored = Store(tmp_path)
        assert restored.started
        assert restored.all("SELECT name,url FROM channel") == [
            {"name": "Target", "url": "https://www.twitch.tv/target"}
        ]
        restored.db.close()

    asyncio.run(exercise())


def test_score_accounts_for_audience_and_nonoverlapping_history():
    broad = {str(i) for i in range(30)}
    detectors = [Detector(Detection()), Detector(Detection())]
    for detector in detectors:
        for t in range(75):
            assert detector.tick(t, Bucket(20, broad)) is None
        assert detector.metrics["comparedWindows"] == 14
        assert detector.metrics["score"] == 0

    broad_events, concentrated_events = [], []
    for t in range(75, 82):
        for detector, authors, events in zip(
            detectors, (broad, {"one"}), (broad_events, concentrated_events)
        ):
            if outcome := detector.tick(t, Bucket(40, authors)):
                events.append(outcome)

    assert [event for event, _ in broad_events] == ["start"]
    assert not concentrated_events
    assert 0 < detectors[1].metrics["score"] < detectors[0].metrics["score"] < 100
    assert detectors[0].metrics["similarWindows"] < detectors[0].metrics["comparedWindows"]
    detectors[0].tick(82, None)
    assert detectors[0].metrics == {}


def test_retention_preserves_manual_and_favorite_clips(tmp_path, monkeypatch):
    monkeypatch.setenv("BLYNX_SERVER_PRUNE", "1")

    async def exercise():
        store = Store(tmp_path)
        engine = Engine(store)
        clips = store.clips_dir()
        clips.mkdir()
        for index, (name, score, favorite, review) in enumerate((
            ("manual", 0, 0, "New"), ("favorite", 75, 1, "New"),
            ("reviewed", 60, 0, "Reviewed"), ("older", 55, 0, "New"),
            ("newer", 50, 0, "New"),
        )):
            path = clips / (name + ".mp4")
            path.write_bytes(b"video")
            store.execute("""INSERT INTO clip(id,channel,name,platform,broadcast,source,onset,score,
                offset,status,path,bytes,favorite,review) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (name, "channel", name, "Twitch", "broadcast", "", index, score,
                 0, "ready", str(path), 5, favorite, review))

        assert engine.prune_clips(clips, 5) == 5
        assert not (clips / "older.mp4").exists()
        assert all((clips / (name + ".mp4")).exists() for name in ("manual", "favorite", "reviewed", "newer"))
        await engine.shutdown()
        store.db.close()

    asyncio.run(exercise())
