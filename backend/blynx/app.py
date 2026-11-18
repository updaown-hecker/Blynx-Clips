import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import signal
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .engine import Engine, public_error
from .media import run
from .platforms import PlatformError, Twitch, YouTube, write_secret
from .server import dashboard_bind, private_ipv4
from .store import Settings, Store
from .tab_capture import save_tab_capture

FRONTEND = Path(os.getenv("BLYNX_FRONTEND", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
ORIGIN = os.getenv("BLYNX_LOCAL_ORIGIN", "http://127.0.0.1:17865")
HOST = urlsplit(ORIGIN).netloc
PUBLIC_ORIGIN = os.getenv("BLYNX_PUBLIC_ORIGIN", "").strip().rstrip("/")
PUBLIC_HOST = urlsplit(PUBLIC_ORIGIN).netloc
LAN_ENABLED = dashboard_bind() == "0.0.0.0"
LAN_PORT = os.getenv("BLYNX_PORT", "17865")
if PUBLIC_ORIGIN:
    parts = urlsplit(PUBLIC_ORIGIN)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment or PUBLIC_HOST == HOST):
        raise ValueError("BLYNX_PUBLIC_ORIGIN must be a separate HTTPS origin, without a path")
COOKIE = "blynx_clips_server_session" if os.getenv("BLYNX_SERVER_MODE") == "1" else "blynx_clips_session"
SESSION = secrets.token_urlsafe(32)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store.default()
    store.import_cut_clips()
    handler = RotatingFileHandler(store.root / "blynx.log", maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("blynx")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    engine = Engine(store)
    await engine.load_credentials()
    app.state.engine = engine
    app.state.pair_code = ("", 0.0)
    supervisor = asyncio.create_task(engine.supervisor())
    app.state.supervisor = supervisor

    def supervisor_finished(task: asyncio.Task) -> None:
        if not engine.stop and not task.cancelled():
            error = task.exception()
            logger.error("Monitoring stopped unexpectedly", exc_info=error)
            if os.getenv("BLYNX_SERVER_MODE") == "1":
                os.kill(os.getpid(), signal.SIGTERM)

    supervisor.add_done_callback(supervisor_finished)
    try:
        yield
    finally:
        await engine.shutdown()
        try:
            await asyncio.wait_for(asyncio.gather(supervisor, return_exceptions=True), timeout=20)
        except TimeoutError:
            pass
        store.db.close()
        logger.removeHandler(handler)
        handler.close()


app = FastAPI(title="Blynx Clips", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
if (FRONTEND / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")


def session_token(origin: str) -> str:
    return hmac.new(SESSION.encode(), origin.encode(), hashlib.sha256).hexdigest()


def safe_cookie(request: Request | WebSocket) -> bool:
    origin = dashboard_origin(request.headers.get("host", ""))
    return hmac.compare_digest(request.cookies.get(COOKIE, "").encode(), session_token(origin).encode())


def dashboard_origin(host: str) -> str:
    if host == HOST:
        return ORIGIN
    if PUBLIC_HOST and host == PUBLIC_HOST:
        return PUBLIC_ORIGIN
    if LAN_ENABLED:
        address, _, port = host.partition(":")
        if port == LAN_PORT and private_ipv4(address):
            return f"http://{host}"
    return ""


@app.middleware("http")
async def local(request: Request, next_call):
    host = request.headers.get("host", "")
    request_origin = request.headers.get("origin", "")
    path = request.url.path
    extension = path.startswith("/api/extension/")
    extension_origin = request_origin.startswith("chrome-extension://")
    origin = dashboard_origin(host)
    if not origin:
        return JSONResponse({"detail": "Dashboard host is not configured"}, 403)
    remote = origin != ORIGIN
    if remote and (extension or path.startswith("/api/pair")):
        return JSONResponse({"detail": "Browser extension pairing requires local access"}, 403)
    if extension and (extension_origin or not request_origin):
        if request.method == "OPTIONS":
            response = JSONResponse({}, 200)
        elif path == "/api/extension/pair" and request.method == "POST":
            response = await next_call(request)
        else:
            raw = request.headers.get("authorization", "").removeprefix("Bearer ")
            digest = hashlib.sha256(raw.encode()).hexdigest()
            store = app.state.engine.store
            expected = store.config("extension_key", "")
            paired_origin = store.config("extension_origin", "")
            origin_matches = not request_origin or not paired_origin or paired_origin == request_origin
            authorized = raw and origin_matches and hmac.compare_digest(digest, expected)
            response = await next_call(request) if authorized else JSONResponse(
                {"detail": "Pair this extension from the Blynx Clips dashboard."}, 401)
        response.headers.update({"Access-Control-Allow-Origin": request_origin, "Vary": "Origin",
                                 "Access-Control-Allow-Headers": "Authorization,Content-Type",
                                 "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                                 "Access-Control-Allow-Private-Network": "true"})
    else:
        if extension:
            return JSONResponse({"detail": "Use the Chrome extension"}, 403)
        if path.startswith(("/api/", "/media/")) and path != "/api/health":
            if not safe_cookie(request):
                return JSONResponse({"detail": "Open the local dashboard first"}, 401)
            if request.method not in ("GET", "HEAD") and (
                request_origin != origin or request.headers.get("x-blynx-local") != "1"
            ):
                return JSONResponse({"detail": "Local dashboard origin and request header are required"}, 403)
            if request_origin and request_origin != origin:
                return JSONResponse({"detail": "Cross-origin requests are disabled"}, 403)
        response = await next_call(request)
    response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                             "X-Frame-Options": "DENY", "Cache-Control": "no-store",
                             "Content-Security-Policy": "default-src 'self'; img-src 'self' https: data:; "
                             "style-src 'self' 'unsafe-inline'; connect-src 'self' " + origin.replace("http", "ws", 1) +
                             "; media-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response


@app.get("/")
async def index(request: Request):
    if not (FRONTEND / "index.html").is_file():
        return JSONResponse({"detail": "Frontend is missing. Run npm ci && npm run build in frontend."}, 503)
    origin = dashboard_origin(request.headers.get("host", ""))
    response = FileResponse(FRONTEND / "index.html")
    response.set_cookie(COOKIE, session_token(origin), httponly=True,
                        secure=origin.startswith("https://"), samesite="strict")
    return response


@app.post("/", include_in_schema=False)
async def legacy_sign_in():
    # Browsers can resubmit the former sign-in form when reloading an open tab.
    return RedirectResponse("/", status_code=303)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse({"detail": [{"msg": error["msg"], "loc": list(error["loc"])}
                                    for error in exc.errors()]}, 422)


@app.get("/api/health")
async def health():
    if app.state.supervisor.done():
        raise HTTPException(503, "Monitoring service stopped")
    return {"app": "blynx-clips", "version": "0.2.0"}


@app.get("/api/state")
async def state():
    return app.state.engine.snapshot()


@app.websocket("/ws")
async def websocket(socket: WebSocket):
    origin = dashboard_origin(socket.headers.get("host", ""))
    if not origin or socket.headers.get("origin") != origin or not safe_cookie(socket):
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        while True:
            await socket.send_json(app.state.engine.snapshot())
            await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError):
        pass


class Link(BaseModel):
    url: str = Field(min_length=3, max_length=1200)


class ChannelChange(BaseModel):
    priority: int | None = Field(None, ge=0, le=100)
    offset: float | None = Field(None, ge=-40, le=40)
    enabled: bool | None = None
    preset: str | None = Field(None, pattern=r"^(|High|Balanced|Low|Manual)$")


@app.post("/api/channels")
async def add_channel(body: Link):
    try:
        return await app.state.engine.add(body.url)
    except Exception as exc:
        raise HTTPException(422, public_error(exc)) from None


@app.patch("/api/channels/{key}")
async def update_channel(key: str, body: ChannelChange):
    engine: Engine = app.state.engine
    current = engine.store.item("SELECT * FROM channel WHERE id=?", (key,))
    if not current:
        raise HTTPException(404, "Channel not found")
    values = current | body.model_dump(exclude_none=True)
    engine.store.execute("UPDATE channel SET priority=?,offset=?,enabled=?,preset=? WHERE id=?",
                         (values["priority"], values["offset"], values["enabled"], values["preset"], key))
    if {"offset", "preset"}.intersection(body.model_fields_set) and key in engine.workers:
        task, _ = engine.workers[key]
        if not task.cancelling():
            task.cancel()
    engine.wake.set()
    return {"ok": True}


@app.delete("/api/channels/{key}")
async def remove_channel(key: str):
    engine: Engine = app.state.engine
    engine.store.execute("DELETE FROM channel WHERE id=?", (key,))
    engine.live.pop(key, None)
    engine.wake.set()
    return {"ok": True}


@app.post("/api/refresh")
async def refresh():
    engine: Engine = app.state.engine
    engine.retry.clear()
    engine.invalidate()
    return {"ok": True}


@app.post("/api/monitor/{command}")
async def monitoring(command: str):
    engine: Engine = app.state.engine
    if command not in {"start", "stop"}:
        raise HTTPException(404, "Unknown monitoring command")
    engine.running = command == "start"
    if not engine.running:
        engine.demo = False
    engine.store.set("running", engine.running)
    engine.invalidate()
    return {"running": engine.running}


@app.post("/api/simulation")
async def simulation():
    engine: Engine = app.state.engine
    if engine.demo or len(engine.workers) >= engine.store.settings.slots:
        raise HTTPException(409, "Simulation already running or all capture slots are occupied")
    engine.retry.pop(engine.demo_id, None)
    engine.demo = True
    engine.wake.set()
    return {"ok": True}


@app.put("/api/settings")
async def settings(body: Settings):
    engine: Engine = app.state.engine
    if body.output:
        path = Path(body.output).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise HTTPException(422, "Cannot create the clip output directory") from None
        if not os.access(path, os.W_OK | os.X_OK):
            raise HTTPException(422, "The clip output directory is not writable")
    engine.store.save_settings(body)
    engine.invalidate()
    return body


class TwitchCredentials(BaseModel):
    client_id: str = Field(pattern=r"^[a-zA-Z0-9]{15,80}$")
    token: str = Field("", max_length=2048, repr=False)


class YouTubeCredentials(BaseModel):
    mode: str = Field(pattern=r"^(api-key|access-token)$")
    value: str = Field(min_length=10, max_length=2048, repr=False)


@app.put("/api/integrations/twitch")
async def save_twitch(body: TwitchCredentials):
    engine: Engine = app.state.engine
    async with engine.credential_lock:
        try:
            token = body.token.strip().removeprefix("oauth:").removeprefix("Bearer ")
            candidate = Twitch(engine.http)
            candidate.client_id = body.client_id
            candidate.token = token
            if token:
                await candidate.validate(True, refresh=False)
                await candidate.secure_store({"access_token": token})
            if engine.auth_task and not engine.auth_task.done():
                engine.auth_task.cancel()
                await asyncio.gather(engine.auth_task, return_exceptions=True)
            if token or candidate.client_id != engine.twitch.client_id:
                engine.twitch = candidate
            engine.store.set("twitch_client_id", body.client_id)
            engine.auth_state = "Twitch connected" if token else "Client ID saved. Authorize Twitch to connect."
            engine.restart_platform("Twitch")
        except Exception as exc:
            raise HTTPException(422, public_error(exc)) from None
    return {"ok": True}


@app.put("/api/integrations/youtube")
async def save_youtube(body: YouTubeCredentials):
    engine: Engine = app.state.engine
    async with engine.credential_lock:
        try:
            value = body.value.strip().removeprefix("Bearer ")
            candidate = YouTube(engine.http, engine.store.quota)
            candidate.api_key = value if body.mode == "api-key" else ""
            candidate.oauth_token = value if body.mode == "access-token" else ""
            await candidate.request("channels", {"part": "id", "forHandle": "@YouTube"})
            await write_secret("Blynx Clips YouTube", body.mode, value)
            engine.store.set("youtube_saved", body.mode)
            engine.youtube = candidate
            engine.youtube_auth = "YouTube connected"
            engine.restart_platform("YouTube")
        except Exception as exc:
            raise HTTPException(422, public_error(exc)) from None
    return {"ok": True}


@app.post("/api/integrations/{platform}/test")
async def test_platform(platform: str):
    engine: Engine = app.state.engine
    try:
        if platform == "twitch":
            await engine.twitch.validate(True)
            engine.auth_state = "Twitch connected"
        elif platform == "youtube":
            await engine.youtube.request("channels", {"part": "id", "forHandle": "@YouTube"})
            engine.youtube_auth = "YouTube connected"
        else:
            raise HTTPException(404, "Unknown integration")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, public_error(exc)) from None
    return {"ok": True}


@app.post("/api/integrations/twitch/authorize")
async def twitch_authorize():
    engine: Engine = app.state.engine
    try:
        device = await engine.twitch.device()
    except Exception as exc:
        raise HTTPException(422, public_error(exc)) from None
    if engine.auth_task:
        engine.auth_task.cancel()

    async def wait_for_authorization():
        engine.auth_state = "Waiting for Twitch approval"
        try:
            await engine.twitch.authorize(device)
            engine.auth_state = "Twitch connected"
            engine.invalidate()
        except Exception as exc:
            engine.auth_state = public_error(exc)

    engine.auth_task = asyncio.create_task(wait_for_authorization())
    return {k: device[k] for k in ("user_code", "verification_uri")}


@app.post("/api/pair")
async def pair():
    code = "".join(secrets.choice("23456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(10))
    app.state.pair_code = (code, time.monotonic() + 300)
    return {"code": code, "expiresIn": 300}


class PairCode(BaseModel):
    code: str = Field(min_length=10, max_length=12, repr=False)


@app.post("/api/extension/pair")
async def redeem_pairing(body: PairCode, request: Request):
    code, expires = app.state.pair_code
    entered = body.code.upper().replace("-", "").replace(" ", "")
    if not entered.isascii() or expires <= time.monotonic() or not hmac.compare_digest(entered, code):
        raise HTTPException(401, "Pairing code expired or incorrect. Generate a new code in Blynx Clips.")
    app.state.pair_code = ("", 0.0)
    token = secrets.token_urlsafe(32)
    store = app.state.engine.store
    store.set("extension_key", hashlib.sha256(token.encode()).hexdigest())
    store.set("extension_origin", request.headers.get("origin", ""))
    return {"token": token}


@app.get("/api/pair/status")
async def pair_status():
    return {"paired": bool(app.state.engine.store.config("extension_key", ""))}


@app.delete("/api/pair")
async def revoke_pairing():
    app.state.engine.store.set("extension_key", "")
    app.state.engine.store.set("extension_origin", "")
    app.state.pair_code = ("", 0.0)
    return {"ok": True}


@app.get("/api/extension/status")
async def extension_status(url: str = ""):
    engine: Engine = app.state.engine
    current = None
    if url:
        try:
            channel = engine.current_channel(url)
        except PlatformError:
            channel = None
        if channel:
            key = channel["id"]
            state = engine.status.get(key, {})
            current = {"name": channel["name"], "recording": state.get("recording", "Idle"),
                       "canClip": key in engine.manual_clips and state.get("health", 0) >= 5}
    return {"running": engine.running, "active": len(engine.workers), "channel": current}


@app.post("/api/extension/tab-recordings")
async def tab_recording(request: Request, url: str):
    return await save_tab_capture(app.state.engine, request, url)


@app.post("/api/extension/clips")
async def extension_clip(body: Link):
    engine: Engine = app.state.engine
    try:
        channel = engine.current_channel(body.url)
        if not channel or channel["id"] not in engine.manual_clips:
            raise PlatformError("This stream needs an active Blynx Clips recording first")
        return engine.manual_clips[channel["id"]]()
    except PlatformError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/channels/{key}/clip")
async def manual_clip(key: str):
    engine: Engine = app.state.engine
    if key not in engine.manual_clips:
        raise HTTPException(409, "This stream has no active recording buffer")
    try:
        return engine.manual_clips[key]()
    except PlatformError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/extension/channels")
async def extension_add(body: Link):
    return await add_channel(body)


def clip(key: str) -> dict:
    result = app.state.engine.store.item("SELECT * FROM clip WHERE id=?", (key,))
    if not result:
        raise HTTPException(404, "Highlight not found")
    return result


class Edit(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=180)
    favorite: bool | None = None
    review: str | None = Field(None, pattern=r"^(New|Reviewed|Approved|Exported|Rejected)$")


@app.patch("/api/clips/{key}")
async def edit_clip(key: str, body: Edit):
    original = clip(key)
    values = original | body.model_dump(exclude_none=True)
    app.state.engine.store.execute("UPDATE clip SET name=?,review=?,favorite=? WHERE id=?",
                                   (values["name"], values["review"], values["favorite"], key))
    return {"ok": True}


@app.delete("/api/clips/{key}")
async def delete_clip(key: str):
    original = clip(key)
    if original["status"] in {"capturing", "processing"}:
        raise HTTPException(409, "This clip is still being created")
    path = Path(original["path"])
    if path.is_file():
        path.unlink()
        path.with_suffix(".jpg").unlink(missing_ok=True)
    app.state.engine.store.execute("DELETE FROM clip WHERE id=?", (key,))
    return {"ok": True}


@app.get("/media/{key}/{asset}")
async def media(key: str, asset: str):
    original = clip(key)
    if original["status"] != "ready" or asset not in {"video", "thumbnail", "download"}:
        raise HTTPException(404, "Media unavailable")
    path = Path(original["path"]).with_suffix(".jpg" if asset == "thumbnail" else ".mp4")
    if not path.is_file():
        raise HTTPException(404, "File is missing")
    return FileResponse(path, filename=path.name if asset == "download" else None)


class Export(BaseModel):
    preset: str = Field("original", pattern=r"^(original|vertical)$")
    start: float = Field(0, ge=0, le=300)
    end: float | None = Field(None, ge=1, le=300)

    @model_validator(mode="after")
    def duration(self):
        if self.end is not None and self.end - self.start < 1:
            raise ValueError("Select at least one second of video")
        return self


@app.get("/api/cut-clips")
async def cut_clips():
    rows = app.state.engine.store.all("SELECT * FROM cut_clip ORDER BY created DESC, id")
    return [{key: value for key, value in row.items() if key != "path"}
            for row in rows if Path(row["path"]).is_file()]


@app.get("/media/cut-clips/{key}/download")
async def download_cut_clip(key: str):
    row = app.state.engine.store.item("SELECT path FROM cut_clip WHERE id=?", (key,))
    if row:
        path = Path(row["path"])
        if path.is_file():
            return FileResponse(path, media_type="video/mp4", filename=path.name)
    raise HTTPException(404, "Cut clip is unavailable")


@app.post("/api/clips/{key}/export")
async def export_clip(key: str, body: Export):
    original = clip(key)
    engine: Engine = app.state.engine
    source = Path(original["path"])
    if original["status"] != "ready" or not source.is_file():
        raise HTTPException(404, "Clip is unavailable")
    end = min(body.end or original["duration"], original["duration"])
    if end - body.start < 1:
        raise HTTPException(422, "Trim range exceeds clip duration")
    folder = engine.store.clips_dir() / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    cut_id = secrets.token_hex(16)
    target = folder / f"{source.stem}_{body.preset}_{cut_id}.mp4"
    temp = target.with_suffix(".partial.mp4")
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-ss", str(body.start), "-t", str(end - body.start), "-map", "0:v:0", "-map", "0:a:0?"]
    if body.preset == "vertical":
        args += ["-vf", "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920:flags=lanczos,setsar=1"]
    try:
        async with engine.encode:
            await run(*args, "-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-c:a", "aac",
                      "-movflags", "+faststart", str(temp))
        size = temp.stat().st_size
        if not size:
            raise RuntimeError("FFmpeg produced an empty export")
        temp.replace(target)
        try:
            with engine.store.db:
                engine.store.db.execute("INSERT INTO cut_clip VALUES(?,?,?,?,?,?,?,?)",
                                        (cut_id, str(target), original["name"], original["channel"],
                                         body.preset, time.time(), end - body.start, size))
                engine.store.db.execute("UPDATE clip SET review='Exported' WHERE id=?", (key,))
        except Exception:
            target.unlink(missing_ok=True)
            raise
    except Exception:
        logging.getLogger("blynx").exception("Could not save cut clip")
        raise HTTPException(500, "Could not save cut clip") from None
    finally:
        temp.unlink(missing_ok=True)
    return {"id": cut_id, "path": str(target)}


@app.post("/api/clips/{key}/location")
async def show_location(key: str):
    original = clip(key)
    directory = Path(original["path"]).parent
    if not directory.is_dir():
        raise HTTPException(404, "Output directory is missing")
    try:
        await run("xdg-open", str(directory), timeout=8)
    except Exception:
        raise HTTPException(422, "Unable to open the folder through the desktop session") from None
    return {"path": str(directory)}


@app.get("/api/analytics")
async def analytics(days: int = 7, platform: str = "", channel: str = "", simulated: bool = False):
    store = app.state.engine.store
    start = time.time() - 86400 * min(max(1, days), 366)
    filters = " AND (?='' OR platform=?) AND (?='' OR channel=?) AND simulated=?"
    args = (platform, platform, channel, channel, simulated)
    stats = store.item("SELECT count(CASE WHEN score>0 THEN 1 END) AS events, count(CASE WHEN status='ready' THEN 1 END) AS clips, "
                       "count(CASE WHEN status='failed' THEN 1 END) AS failed, avg(CASE WHEN score_version=2 AND score>0 THEN score END) AS score, "
                       "sum(bytes) AS bytes, avg(CASE WHEN status='ready' THEN duration END) AS duration "
                       "FROM clip WHERE onset>?" + filters, (start, *args))
    sessions = store.item("SELECT sum(max(0,ended-max(started,?)))/3600.0 AS hours "
                          "FROM session WHERE ended>?" + filters, (start, start, *args))
    groups = store.all("SELECT channel, count(*) AS clips, sum(bytes) AS bytes "
                       "FROM clip WHERE status='ready' AND onset>?" + filters +
                       " GROUP BY channel ORDER BY clips DESC", (start, *args))
    peaks = store.all("SELECT id, channel, name, onset, score, score_version AS scoreVersion, status FROM clip WHERE score>0 AND onset>?" +
                      filters + " ORDER BY score_version DESC, score DESC LIMIT 20", (start, *args))
    return {"stats": stats | sessions, "groups": groups, "peaks": peaks}

