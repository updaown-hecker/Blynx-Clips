import json
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from blynx.app import ORIGIN, app

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
