import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from blynx import app as dashboard
from blynx.server import dashboard_bind


@pytest.mark.parametrize("origin", ("https://clips.example.test", "http://192.168.4.72:17866"))
def test_dashboard_opens_directly_and_enforces_same_origin(origin, tmp_path, monkeypatch):
    monkeypatch.setenv("BLYNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BLYNX_WEB_PASSWORD", "obsolete")
    monkeypatch.setattr(dashboard, "PUBLIC_ORIGIN", "https://clips.example.test")
    monkeypatch.setattr(dashboard, "PUBLIC_HOST", "clips.example.test")
    monkeypatch.setattr(dashboard, "LAN_ENABLED", True)
    monkeypatch.setattr(dashboard, "LAN_PORT", "17866")
    monkeypatch.setattr(dashboard, "FRONTEND", tmp_path)
    (tmp_path / "index.html").write_text("<title>Blynx Clips dashboard</title>")

    ws = origin.replace("http", "ws", 1) + "/ws"
    headers = {"Origin": origin, "X-Blynx-Local": "1"}
    with TestClient(dashboard.app, base_url=origin) as client:
        assert client.get("/api/state").status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(ws, headers={"Origin": origin}):
                pass

        index = client.get("/")
        assert index.status_code == 200
        assert "Blynx Clips dashboard" in index.text
        assert "Dashboard password" not in index.text
        cookie = index.headers["set-cookie"]
        assert (("Secure" in cookie) == origin.startswith("https://"))
        assert "HttpOnly" in cookie
        assert client.get("/api/state").status_code == 200
        old_form = client.post("/", data={"password": "old"}, follow_redirects=False)
        assert old_form.status_code == 303 and old_form.headers["location"] == "/"
        assert "Blynx Clips dashboard" in client.post("/", data={"password": "old"}).text
        assert client.get("/api/state", headers={"Host": "unconfigured.example.test"}).status_code == 403
        assert client.post("/api/monitor/stop", headers=headers | {"Origin": "https://wrong.test"}).status_code == 403
        assert client.post("/api/pair", headers=headers).status_code == 403
        assert client.post("/api/monitor/stop", headers=headers).json() == {"running": False}
        assert client.get(dashboard.ORIGIN + "/api/state", headers={"Cookie": cookie.split(";")[0]}).status_code == 401

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(ws, headers={"Origin": "https://wrong.test"}):
                pass
        with client.websocket_connect(ws, headers={"Origin": origin}) as socket:
            assert "channels" in socket.receive_json()


def test_server_listens_on_lan_without_password(monkeypatch):
    monkeypatch.delenv("BLYNX_WEB_PASSWORD", raising=False)
    monkeypatch.setenv("BLYNX_SERVER_MODE", "1")
    assert dashboard_bind() == "0.0.0.0"
    monkeypatch.delenv("BLYNX_SERVER_MODE")
    assert dashboard_bind() == "127.0.0.1"
