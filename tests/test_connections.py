import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from blynx.app import ORIGIN, app
from blynx.editor import stable_camera

DASHBOARD = {"origin": ORIGIN, "x-blynx-local": "1"}
EXTENSION = {"origin": "chrome-extension://blynx-test"}
VIDEO = "https://www.youtube.com/live/aqz-KE-bpKQ"


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("BLYNX_DATA_DIR", str(tmp_path))
    with TestClient(app, base_url=ORIGIN) as client:
        client.get("/")
        yield client


def pair(client):
    code = client.post("/api/pair", headers=DASHBOARD).json()["code"]
    body = client.post("/api/extension/pair", headers=EXTENSION, json={"code": code})
    assert body.status_code == 200
    assert client.post("/api/extension/pair", headers=EXTENSION, json={"code": code}).status_code == 401
    return {**EXTENSION, "authorization": "Bearer " + body.json()["token"]}


def test_extension_pair_scope_and_revoke(local):
    assert local.post("/api/pair").status_code == 403
    headers = pair(local)
    assert local.get("/api/extension/status", headers=headers).json()["active"] == 0
    denied = {**headers, "origin": "chrome-extension://another-extension"}
    assert local.get("/api/extension/status", headers=denied).status_code == 401
    assert local.get("/api/pair/status", headers=DASHBOARD).json()["paired"]
    assert local.post("/api/extension/clips", headers=headers, json={"url": VIDEO}).status_code == 409
    local.delete("/api/pair", headers=DASHBOARD)
    assert local.get("/api/extension/status", headers=headers).status_code == 401


def test_credentials_validate_before_saving_and_never_return_secrets(local, monkeypatch):
    existing = app.state.engine.http
    secrets = {}

    async def write_secret(service, key, value):
        secrets[(service, key)] = value

    async def validate_twitch(request):
        if request.headers["authorization"] != "OAuth a-good-twitch-token":
            return httpx.Response(401)
        return httpx.Response(200, json={"client_id": "twitchtestclient1234", "scopes": ["user:read:chat"], "user_id": "123"})

    async def fake_upstream(request):
        if request.url.host == "id.twitch.tv":
            return await validate_twitch(request)
        if request.headers.get("x-goog-api-key") == "good-youtube-key" or request.headers.get("authorization") == "Bearer good-youtube-token":
            return httpx.Response(200, json={"items": [{"id": "channel"}]})
        return httpx.Response(403, json={"error": {"errors": [{"reason": "keyInvalid"}]}})

    async def fake_store_twitch(self, record):
        await write_secret("Twitch", self.client_id, record["access_token"])

    monkeypatch.setattr("blynx.app.write_secret", write_secret)
    monkeypatch.setattr("blynx.platforms.Twitch.secure_store", fake_store_twitch)
    app.state.engine.http = httpx.AsyncClient(transport=httpx.MockTransport(fake_upstream))
    try:
        kind = "/api/integrations/twitch"
        twitch = {"client_id": "twitchtestclient1234", "token": "bad-twitch-token"}
        assert local.put(kind, headers=DASHBOARD, json=twitch).status_code == 422
        assert not secrets
        twitch["token"] = "a-good-twitch-token"
        assert local.put(kind, headers=DASHBOARD, json=twitch).status_code == 200
        kind = "/api/integrations/youtube"
        assert local.put(kind, headers=DASHBOARD, json={"mode": "api-key", "value": "bad-youtube-key"}).status_code == 422
        assert local.put(kind, headers=DASHBOARD, json={"mode": "api-key", "value": "good-youtube-key"}).status_code == 200
        assert local.put(kind, headers=DASHBOARD, json={"mode": "access-token", "value": "good-youtube-token"}).status_code == 200
        state = local.get("/api/state").text
        assert "Twitch connected" in state and "YouTube connected" in state
        assert not any(value in state for value in secrets.values())
    finally:
        # The lifespan closes the original client.
        client = app.state.engine.http
        app.state.engine.http = existing
        local.portal.call(client.aclose)


def test_authenticated_tab_upload_creates_mp4(local, tmp_path):
    headers = pair(local)
    path = tmp_path / "source.webm"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12",
        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", "libvpx", "-threads", "2",
        "-c:a", "libopus", str(path)
    ], check=True)
    endpoint = "/api/extension/tab-recordings"
    headers["content-type"] = "video/webm"
    response = local.post(endpoint, params={"url": VIDEO}, headers=headers, content=path.read_bytes())
    assert response.status_code == 200, response.text
    key = response.json()["id"]
    assert response.json()["duration"] >= 1.9
    assert local.get(f"/media/{key}/video", headers={"range": "bytes=0-511"}).status_code == 206
    assert local.get(f"/media/{key}/thumbnail").status_code == 200


def test_cut_clips_recover_older_exports_and_keep_new_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("BLYNX_DATA_DIR", str(tmp_path))
    folder = tmp_path / "clips"
    exports = folder / "exports"
    exports.mkdir(parents=True)
    source = folder / "highlight.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12",
        "-t", "4", "-c:v", "libx264", "-threads", "2", str(source)
    ], check=True)
    legacy = exports / "highlight_original_abcdef.mp4"
    legacy.write_bytes(source.read_bytes())
    (exports / "highlight_vertical_012345.partial.mp4").write_bytes(b"unfinished")

    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/cut-clips").status_code == 401
        assert client.get("/").status_code == 200
        cuts = client.get("/api/cut-clips").json()
        assert len(cuts) == 1
        legacy_download = client.get(f"/media/cut-clips/{cuts[0]['id']}/download")
        assert legacy_download.content == legacy.read_bytes()
        legacy_edit = client.get(f"/api/cut-clips/{cuts[0]['id']}/edit").json()
        assert legacy_edit["source"]["kind"] == "cut"
        assert legacy_edit["source"]["duration"] == 4
        assert legacy_edit["recipe"] is None

        client.portal.call(app.state.engine.store.execute, """
            INSERT INTO clip(id,channel,name,platform,broadcast,source,onset,score,offset,status,path,duration)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("original", "Streamer", "Highlight title", "Twitch", "live", "", 1, 0, 0, "ready", str(source), 4))
        export = client.post("/api/clips/original/export", headers=DASHBOARD,
                             json={"start": 1, "end": 3, "preset": "vertical"})
        assert export.status_code == 200, export.text
        cut_id = export.json()["id"]
        cut = client.get("/api/cut-clips").json()[0]
        assert (cut["id"], cut["name"], cut["channel"], cut["preset"], cut["duration"]) == (
            cut_id, "Highlight title", "Streamer", "vertical", 2)
        assert "path" not in cut
        assert client.get("/media/original/download").content == legacy.read_bytes()

    with TestClient(app, base_url=ORIGIN) as client:
        client.get("/")
        assert len(client.get("/api/cut-clips").json()) == 2
        download = client.get(f"/media/cut-clips/{cut_id}/download")
        assert download.status_code == 200
        assert download.headers["content-type"] == "video/mp4"
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.content != legacy.read_bytes()
        assert len(download.content) == cut["bytes"]
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration", "-of", "json", "-i", "pipe:0"
        ], input=download.content, capture_output=True, check=True)
        stream = json.loads(probe.stdout)["streams"][0]
        assert (stream["width"], stream["height"], float(stream["duration"])) == (1080, 1920, 2)
        assert client.get("/media/cut-clips/missing/download").status_code == 404


def test_camera_detection_prefers_a_face_that_stays_in_a_corner():
    camera = stable_camera([
        [(0.84, 0.20, 0.08, 0.12), (0.51, 0.48, 0.16, 0.20)],
        [(0.85, 0.21, 0.08, 0.12), (0.20, 0.14, 0.10, 0.15)],
        [(0.83, 0.20, 0.08, 0.12)],
        [],
    ])
    assert camera is not None
    assert camera["x"] < 0.84 < camera["x"] + camera["width"]
    assert camera["y"] < 0.20 < camera["y"] + camera["height"]
    assert stable_camera([[(0.51, 0.48, 0.16, 0.20)]] * 8) is None


def test_editor_analysis_export_and_video_preview(tmp_path, monkeypatch):
    from blynx import editor, media

    monkeypatch.setenv("BLYNX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(editor, "detect_frames", lambda *_: {"x": 0, "y": 0, "width": 0.3, "height": 0.4})
    real_run = media.run

    async def run(*args, **kwargs):
        if args[1:3] == ("-m", "blynx.captions"):
            assert args[-1] == "--short-cues"
            assert args[4] == str(tmp_path / "whisper")
            Path(args[args.index("--output") + 1]).write_text('[[0.0,0.9,"Before trim"],[1.1,2.1,"Edited words"]]')
            return b"Unrelated native library output"
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(media, "run", run)

    def finish(client, response):
        assert response.status_code == 202, response.text
        job_id = response.json()["job"]
        for _ in range(300):
            state = client.get(f"/api/editor-jobs/{job_id}").json()
            assert state["status"] != "error", state
            if state["status"] == "done":
                return state["result"]
            time.sleep(0.05)
        pytest.fail("Editor job timed out")

    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/").status_code == 200
        folder = tmp_path / "clips"
        folder.mkdir(exist_ok=True)
        original = folder / "highlight.mp4"
        source = folder / "highlight.source.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=blue:size=320x180:rate=12,drawbox=0:0:96:72:red:t=fill",
            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3",
            "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(source)
        ], check=True)
        original.write_bytes(b"Already rendered highlight")
        client.portal.call(app.state.engine.store.execute, """
            INSERT INTO clip(id,channel,name,platform,broadcast,source,onset,score,offset,status,path,duration)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("edit", "Streamer", "Sample", "Twitch", "live", "", 1, 0, 0, "ready", str(original), 3))
        assert client.get("/media/edit/editor-source").content == source.read_bytes()
        assert client.get("/media/edit/download").content == original.read_bytes()
        analysis = finish(client, client.post("/api/clips/edit/editor-analysis", headers=DASHBOARD))
        assert len(analysis["cues"]) == 2
        assert not analysis["caption_error"]
        assert analysis["camera"]["width"] == 0.3
        endpoint = "/api/clips/edit/editor-export"
        bad = client.post(endpoint, headers=DASHBOARD, json={"start": 1, "end": 3, "layout": "camera-top"})
        assert bad.status_code == 422
        edited = finish(client, client.post(endpoint, headers=DASHBOARD, json={
            "start": 1, "end": 3, "layout": "camera-top", "camera": analysis["camera"],
            "caption_style": "pop", "cues": analysis["cues"]
        }))
        video_url = f"/media/cut-clips/{edited['id']}"
        preview = client.get(video_url + "/video", headers={"range": "bytes=0-511"})
        assert preview.status_code == 206
        download = client.get(video_url + "/download")
        assert download.headers["content-disposition"].startswith("attachment;")
        path = tmp_path / "result.mp4"
        path.write_bytes(download.content)
        pixels = subprocess.run([
            "ffmpeg", "-v", "error", "-ss", "0.5", "-i", str(path), "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "pipe:1"
        ], capture_output=True, check=True).stdout
        assert len(pixels) == 1080 * 1920 * 3
        top = (320 * 1080 + 540) * 3
        bottom = (960 * 1080 + 540) * 3
        assert pixels[top] > 200 and pixels[top + 2] < 40
        assert pixels[bottom] < 40 and pixels[bottom + 2] > 200
        assert any(pixels[i] > 180 and pixels[i + 1] > 150 and pixels[i + 2] < 60
                   for i in range(1450 * 1080 * 3, 1770 * 1080 * 3, 3))
        assert client.get("/api/cut-clips").json()[0]["id"] == edited["id"]
        assert source.is_file() and original.is_file()

    with TestClient(app, base_url=ORIGIN) as client:
        client.get("/")
        cut_endpoint = f"/api/cut-clips/{edited['id']}"
        reopened = client.get(cut_endpoint + "/edit").json()
        assert reopened["source"]["kind"] == "clip"
        assert reopened["source"]["id"] == "edit"
        assert reopened["recipe"] == {"start": 1, "end": 3, "layout": "camera-top",
                                      "camera": analysis["camera"], "caption_style": "pop", "cues": analysis["cues"]}
        recipe = reopened["recipe"] | {"layout": "camera-bottom", "caption_style": "boxed"}
        recipe["cues"] = [*recipe["cues"][:-1], recipe["cues"][-1] | {"text": "Updated caption"}]
        revision = finish(client, client.post(endpoint, headers=DASHBOARD, json=recipe))
        assert revision["id"] != edited["id"]
        assert client.get(video_url + "/download").content == download.content
        updated = client.get(f"/api/cut-clips/{revision['id']}/edit").json()
        assert updated["recipe"] == recipe
        original.unlink()
        fallback = client.get(cut_endpoint + "/edit").json()
        assert fallback["source"]["id"] == edited["id"]
        assert fallback["source"]["kind"] == "cut" and fallback["recipe"] is None
        assert finish(client, client.post(cut_endpoint + "/editor-export", headers=DASHBOARD, json={
            "end": 1, "layout": "fit", "caption_style": "off"
        }))["id"] != edited["id"]
