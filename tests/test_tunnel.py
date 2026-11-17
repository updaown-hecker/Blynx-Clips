import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from blynx import app as dashboard


def test_forwarded_dashboard_requires_password_and_accepts_only_matching_origin(tmp_path, monkeypatch):
    public = "https://clips.example.test"
    host = "clips.example.test"
    password = "example-token-used-only-in-tests"
    monkeypatch.setenv("BLYNX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dashboard, "PUBLIC_ORIGIN", public)
    monkeypatch.setattr(dashboard, "PUBLIC_HOST", host)
    monkeypatch.setattr(dashboard, "WEB_PASSWORD", password)
    monkeypatch.setattr(dashboard, "FRONTEND", tmp_path)
    (tmp_path / "index.html").write_text("<title>Application</title>")

    with TestClient(dashboard.app, base_url=public) as client:
        login = client.get("/")
        assert "Dashboard password" in login.text and "set-cookie" not in login.headers
        assert client.get("/api/state").status_code == 401
        assert client.get("/media/test/video").status_code == 401
        assert client.get("/", headers={"Host": "unconfigured.example.test"}).status_code == 403
        assert client.get(dashboard.ORIGIN + "/").text == login.text
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"Origin": public}):
                pass

        assert client.post("/", data={"password": password}, headers={"Origin": "https://wrong.test"}).status_code == 403
        invalid = client.post("/", data={"password": "wrong"}, headers={"Origin": public})
        assert invalid.status_code == 401 and "set-cookie" not in invalid.headers
        logged_in = client.post("/", data={"password": password}, headers={"Origin": public}, follow_redirects=False)
        assert logged_in.status_code == 303
        cookie = logged_in.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie
        assert "Application" in client.get("/").text
        assert "wss://clips.example.test" in client.get("/").headers["content-security-policy"]
        assert client.get("/api/state").status_code == 200
        assert client.post("/api/pair", headers={"Origin": public, "X-Blynx-Local": "1"}).status_code == 403
        assert client.post("/api/monitor/stop", headers={"Origin": "https://wrong.test", "X-Blynx-Local": "1"}).status_code == 403
        response = client.post("/api/monitor/stop", headers={"Origin": public, "X-Blynx-Local": "1"})
        assert response.json() == {"running": False}
        local = client.post(dashboard.ORIGIN + "/", data={"password": password},
                            headers={"Origin": dashboard.ORIGIN}, follow_redirects=False)
        assert local.status_code == 303 and "secure" not in local.headers["set-cookie"].lower()
        assert client.get(dashboard.ORIGIN + "/api/state").json()["running"] is False
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"Origin": "https://wrong.test"}):
                pass
        with client.websocket_connect("wss://" + host + "/ws", headers={"Origin": public}) as socket:
            assert "channels" in socket.receive_json()


def test_lan_sign_in_is_separate_from_public_https(tmp_path, monkeypatch):
    public = "https://clips.example.test"
    lan = "http://192.168.4.73:17866"
    public_password = "public-test-password-32-characters"
    lan_password = "separate-lan-password-32-characters"
    monkeypatch.setenv("BLYNX_DATA_DIR", str(tmp_path))
    for key, value in {"PUBLIC_ORIGIN": public, "PUBLIC_HOST": "clips.example.test",
                       "LAN_ORIGIN": lan, "LAN_HOST": "192.168.4.73:17866",
                       "LAN_PASSWORD": lan_password, "WEB_PASSWORD": public_password}.items():
        monkeypatch.setattr(dashboard, key, value)

    with TestClient(dashboard.app, base_url=lan) as client:
        assert "Dashboard password" in client.get("/").text
        assert client.get("/api/state").status_code == 401
        assert client.post("/", data={"password": public_password}, headers={"Origin": lan}).status_code == 401
        reply = client.post("/", data={"password": lan_password}, headers={"Origin": lan}, follow_redirects=False)
        assert reply.status_code == 303 and "secure" not in reply.headers["set-cookie"].lower()
        assert client.get("/api/state").status_code == 200
        assert client.get(public + "/api/state", headers={"Cookie": reply.headers["set-cookie"].split(";")[0]}).status_code == 401
        with client.websocket_connect(lan.replace("http://", "ws://", 1) + "/ws", headers={"Origin": lan}) as socket:
            assert "channels" in socket.receive_json()


def test_wildcard_listener_accepts_current_private_ip_only(tmp_path, monkeypatch):
    lan = "http://192.168.4.93:17866"
    password = "separate-lan-password-32-characters"
    monkeypatch.setenv("BLYNX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dashboard, "LAN_ADDRESS", "0.0.0.0")
    monkeypatch.setattr(dashboard, "LAN_PORT", "17866")
    monkeypatch.setattr(dashboard, "LAN_HOST", "0.0.0.0:17866")
    monkeypatch.setattr(dashboard, "LAN_ORIGIN", "http://0.0.0.0:17866")
    monkeypatch.setattr(dashboard, "LAN_PASSWORD", password)

    with TestClient(dashboard.app, base_url=lan) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("http://8.8.8.8:17866/api/health").status_code == 403
        assert client.get("http://192.168.4.93:8080/api/health").status_code == 403
        assert client.get("/api/state").status_code == 401
        reply = client.post("/", data={"password": password}, headers={"Origin": lan}, follow_redirects=False)
        assert reply.status_code == 303
        assert client.get("/api/state").status_code == 200
        with client.websocket_connect("ws://192.168.4.93:17866/ws", headers={"Origin": lan}) as socket:
            assert "channels" in socket.receive_json()
