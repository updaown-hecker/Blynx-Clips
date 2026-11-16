import asyncio
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import grpc
import httpx
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from websockets.asyncio.client import connect


class PlatformError(RuntimeError):
    pass


async def read_secret(service: str, account: str) -> str:
    import keyring
    try:
        return await asyncio.to_thread(keyring.get_password, service, account) or ""
    except Exception:
        return ""


async def write_secret(service: str, account: str, value: str) -> None:
    import keyring
    try:
        await asyncio.to_thread(keyring.set_password, service, account, value)
    except Exception:
        raise PlatformError("Credential storage unavailable. Unlock KDE Wallet or start a Secret Service provider.") from None


class QuotaError(PlatformError):
    pass


@dataclass(frozen=True)
class Source:
    platform: str
    kind: str
    key: str
    url: str


@dataclass(frozen=True)
class Live:
    broadcast: str
    url: str
    title: str
    thumbnail: str = ""
    viewers: int | None = None
    chat: str = ""


def parse_url(raw: str) -> Source:
    try:
        url = urlsplit(raw.strip() if "://" in raw else "https://" + raw.strip())
        host = url.hostname
        if url.scheme not in ("http", "https") or url.username or url.password or url.port:
            raise ValueError
    except ValueError:
        raise PlatformError("Enter a valid Twitch or YouTube link") from None
    parts = [part for part in url.path.split("/") if part]
    a = parts[0] if parts else ""
    b = parts[1] if len(parts) > 1 else ""
    if host in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}:
        if a == "videos" and re.fullmatch(r"\d{1,20}", b):
            return Source("Twitch", "video", b, f"https://www.twitch.tv/videos/{b}")
        if re.fullmatch(r"[a-zA-Z0-9_]{1,25}", a) and a.lower() not in {
            "directory", "downloads", "settings", "search", "drops", "subscriptions", "inventory", "videos"
        }:
            return Source("Twitch", "login", a.lower(), f"https://www.twitch.tv/{a.lower()}")
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "gaming.youtube.com"}:
        video = (a if host == "youtu.be" else (parse_qs(url.query).get("v", [""])[0] if a == "watch"
                 else (b if a in {"live", "shorts", "embed"} else "")))
        if re.fullmatch(r"[\w-]{11}", video, re.ASCII):
            return Source("YouTube", "video", video, f"https://www.youtube.com/watch?v={video}")
        if a.startswith("@") and re.fullmatch(r"@[\w.-]{2,40}", a):
            return Source("YouTube", "handle", a, f"https://www.youtube.com/{a}/live")
        if a == "channel" and re.fullmatch(r"UC[\w-]{22}", b, re.ASCII):
            return Source("YouTube", "channel", b, f"https://www.youtube.com/channel/{b}/live")
        if a == "user" and re.fullmatch(r"[\w.-]{1,60}", b, re.ASCII):
            return Source("YouTube", "username", b, f"https://www.youtube.com/user/{b}/live")
    raise PlatformError("Use a supported Twitch or YouTube channel or video link")


def timestamp(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None
    except ValueError:
        return None


ChatCallback = Callable[[str, str, str, bool, float | None], None]


class Twitch:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.http = client
        self.client_id = os.getenv("BLYNX_TWITCH_CLIENT_ID", "")
        self.token = os.getenv("BLYNX_TWITCH_TOKEN", "")
        self.user = ""
        self.validated = 0.0
        self.lock = asyncio.Lock()

    @property
    def headers(self) -> dict[str, str]:
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {self.token}"}

    async def secure_store(self, value: dict) -> None:
        await write_secret("Blynx Clips Twitch", self.client_id, json.dumps(value))

    async def stored(self) -> dict:
        raw = await read_secret("Blynx Clips Twitch", self.client_id)
        return json.loads(raw) if raw else {}

    async def validate(self, force: bool = False, refresh: bool = True) -> str:
        async with self.lock:
            if not force and self.user and time.monotonic() - self.validated < 3500:
                return self.user
            if not self.client_id:
                raise PlatformError("Set up Twitch credentials in Settings")
            stored = await self.stored()
            self.token = self.token or stored.get("access_token", "")
            if not self.token:
                raise PlatformError("Authorize Twitch in Settings")
            result = await self.http.get("https://id.twitch.tv/oauth2/validate",
                                         headers={"Authorization": f"OAuth {self.token}"})
            if refresh and result.status_code == 401 and stored.get("refresh_token"):
                response = await self.http.post("https://id.twitch.tv/oauth2/token", data={
                    "client_id": self.client_id, "grant_type": "refresh_token",
                    "refresh_token": stored["refresh_token"],
                })
                if not response.is_success:
                    raise PlatformError("Twitch token refresh failed. Authorize Twitch again.")
                stored = response.json()
                self.token = stored["access_token"]
                await self.secure_store(stored)
                result = await self.http.get("https://id.twitch.tv/oauth2/validate",
                                             headers={"Authorization": f"OAuth {self.token}"})
            if not result.is_success:
                raise PlatformError(f"Twitch authorization validation failed (HTTP {result.status_code})")
            record = result.json()
            if record["client_id"] != self.client_id or "user:read:chat" not in record["scopes"]:
                raise PlatformError("Twitch token needs the configured client ID and user:read:chat scope")
            self.user = record["user_id"]
            self.validated = time.monotonic()
            return self.user

    async def device(self) -> dict:
        if not self.client_id:
            raise PlatformError("Enter your public Twitch application Client ID in Settings first")
        result = await self.http.post("https://id.twitch.tv/oauth2/device",
                                      data={"client_id": self.client_id, "scopes": "user:read:chat"})
        if not result.is_success:
            raise PlatformError(f"Twitch device authorization failed (HTTP {result.status_code})")
        return result.json()

    async def authorize(self, device: dict) -> None:
        deadline = time.monotonic() + device["expires_in"]
        interval = max(5, device["interval"])
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            response = await self.http.post("https://id.twitch.tv/oauth2/token", data={
                "client_id": self.client_id, "scopes": "user:read:chat",
                "device_code": device["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            if response.is_success:
                tokens = response.json()
                async with self.lock:
                    await self.secure_store(tokens)
                    self.token = tokens["access_token"]
                    self.validated = 0
                await self.validate(True)
                return
            try:
                reason = response.json().get("message", "")
            except ValueError:
                reason = ""
            if response.status_code == 429 or reason == "slow_down":
                interval += 5
            elif reason != "authorization_pending":
                raise PlatformError("Twitch rejected the device authorization request")
        raise PlatformError("Twitch device authorization expired")

    async def request(self, endpoint: str, params: list | dict | None = None, payload: dict | None = None) -> dict:
        await self.validate()
        for attempt in range(2):
            result = await self.http.request("POST" if payload is not None else "GET",
                                             f"https://api.twitch.tv/helix/{endpoint}", params=params,
                                             json=payload, headers=self.headers)
            if result.status_code != 401 or attempt:
                break
            await self.validate(True)
        if not result.is_success:
            raise PlatformError(f"Twitch {endpoint} failed (HTTP {result.status_code})")
        return result.json()

    async def resolve(self, source: Source) -> tuple[str, str, str]:
        if source.kind == "video":
            videos = (await self.request("videos", {"id": source.key}))["data"]
            if not videos:
                raise PlatformError("Twitch VOD was not found or is unavailable")
            params = {"id": videos[0]["user_id"]}
        else:
            params = {"login": source.key}
        users = (await self.request("users", params))["data"]
        if not users:
            raise PlatformError("Twitch broadcaster was not found")
        channel = users[0]
        return channel["id"], channel["display_name"], channel["profile_image_url"]

    async def live(self, channels: list[str]) -> dict[str, Live]:
        result = {}
        for start in range(0, len(channels), 100):
            batch = channels[start:start + 100]
            data = (await self.request("streams", [("first", 100), *[("user_id", key) for key in batch]]))["data"]
            result.update({s["user_id"]: Live(s["id"], f'https://www.twitch.tv/{s["user_login"]}',
                            s["title"], s["thumbnail_url"].replace("{width}", "640").replace("{height}", "360"),
                            s["viewer_count"], s["user_id"]) for s in data})
        return result

    async def _subscribe(self, broadcaster: str, session: str) -> None:
        user = await self.validate()
        await self.request("eventsub/subscriptions", payload={
            "type": "channel.chat.message", "version": "1",
            "condition": {"broadcaster_user_id": broadcaster, "user_id": user},
            "transport": {"method": "websocket", "session_id": session},
        })

    async def chat(self, live: Live, message: ChatCallback, status: Callable[[str], None]) -> None:
        socket = await connect("wss://eventsub.wss.twitch.tv/ws", ping_interval=None, open_timeout=15)
        try:
            welcome = json.loads(await asyncio.wait_for(socket.recv(), 10))["payload"]["session"]
            await self._subscribe(live.chat, welcome["id"])
            keepalive = welcome.get("keepalive_timeout_seconds") or 10
            status("EventSub connected")
            while True:
                data = json.loads(await asyncio.wait_for(socket.recv(), keepalive + 5))
                meta, payload = data["metadata"], data["payload"]
                kind = meta["message_type"]
                if kind == "notification":
                    event = payload["event"]
                    body = event.get("message", {})
                    message(meta["message_id"], event["chatter_user_id"], body.get("text", ""),
                            any(f["type"] == "emote" for f in body.get("fragments", [])),
                            timestamp(meta.get("message_timestamp")))
                elif kind == "session_reconnect":
                    url = payload["session"]["reconnect_url"]
                    parsed = urlsplit(url)
                    if parsed.scheme != "wss" or parsed.hostname != "eventsub.wss.twitch.tv":
                        raise PlatformError("Twitch supplied an invalid reconnect URL")
                    replacement = await connect(url, ping_interval=None, open_timeout=15)
                    try:
                        welcome = json.loads(await asyncio.wait_for(replacement.recv(), 10))["payload"]["session"]
                    except BaseException:
                        await replacement.close()
                        raise
                    await socket.close()
                    socket = replacement
                    keepalive = welcome.get("keepalive_timeout_seconds") or keepalive
                elif kind == "revocation":
                    raise PlatformError("Twitch chat subscription revoked: " + payload["subscription"]["status"])
        finally:
            await socket.close()


def _chat_wire() -> tuple:
    # Wire subset of the schema in Google's Streaming Live Chat reference.
    types = descriptor_pb2.FieldDescriptorProto
    schema = descriptor_pb2.FileDescriptorProto(name="live_wire.proto", package="youtube.api.v3", syntax="proto2")
    definitions = {
        "Request": [("live_chat_id", 1, "string"), ("page_token", 99, "string"), ("part", 100, "*string")],
        "Response": [("offline_at", 2, "string"), ("next_page_token", 100602, "string"), ("items", 1007, "*Item")],
        "Item": [("id", 101, "string"), ("snippet", 2, "Snippet")],
        "Snippet": [("type", 1, "int32"), ("author_channel_id", 301, "string"),
                    ("published_at", 4, "string"), ("display_message", 16, "string")],
    }
    for name, fields in definitions.items():
        descriptor = schema.message_type.add(name=name)
        for key, number, kind in fields:
            repeated = kind.startswith("*")
            kind = kind.lstrip("*")
            field = descriptor.field.add(name=key, number=number,
                                         label=types.LABEL_REPEATED if repeated else types.LABEL_OPTIONAL)
            if kind in ("string", "int32"):
                field.type = types.TYPE_STRING if kind == "string" else types.TYPE_INT32
            else:
                field.type = types.TYPE_MESSAGE
                field.type_name = f".youtube.api.v3.{kind}"
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return tuple(message_factory.GetMessageClass(pool.FindMessageTypeByName(f"youtube.api.v3.{name}"))
                 for name in ("Request", "Response"))


class YouTube:
    def __init__(self, client: httpx.AsyncClient, consume: Callable[[str, int], None]) -> None:
        self.http = client
        self.api_key = os.getenv("BLYNX_YOUTUBE_API_KEY", "")
        self.oauth_token = ""
        self.consume = consume
        self.Request, self.Response = _chat_wire()
        self.cursors: dict[str, str] = {}

    @property
    def headers(self) -> dict[str, str]:
        if self.oauth_token:
            return {"Authorization": f"Bearer {self.oauth_token}"}
        return {"X-Goog-Api-Key": self.api_key}

    async def request(self, resource: str, params: dict) -> dict:
        if not (self.api_key or self.oauth_token):
            raise PlatformError("Enter a YouTube API key or OAuth access token in Settings")
        self.consume("search" if resource == "search" else "youtube", 5 if resource == "liveChat/messages" else 1)
        result = await self.http.get(f"https://www.googleapis.com/youtube/v3/{resource}",
                                     params=params, headers=self.headers)
        body = result.json()
        if not result.is_success:
            reasons = body.get("error", {}).get("errors", [])
            reason = reasons[0].get("reason", "") if reasons else ""
            if reason in {"dailyLimitExceeded", "quotaExceeded"}:
                raise QuotaError("YouTube API quota exhausted")
            raise PlatformError(f"YouTube {resource}: {reason or 'HTTP ' + str(result.status_code)}")
        return body

    async def resolve(self, source: Source) -> tuple[str, str, str]:
        if source.kind == "video":
            items = (await self.request("videos", {"part": "snippet", "id": source.key}))["items"]
            if not items:
                raise PlatformError("YouTube video was not found or is unavailable")
            snippet = items[0]["snippet"]
            return snippet["channelId"], snippet["channelTitle"], ""
        key = {"handle": "forHandle", "username": "forUsername", "channel": "id"}[source.kind]
        items = (await self.request("channels", {"part": "snippet", key: source.key}))["items"]
        if not items:
            raise PlatformError("YouTube channel was not found")
        item = items[0]
        return item["id"], item["snippet"]["title"], item["snippet"]["thumbnails"]["default"]["url"]

    async def live(self, channel: str, video: str = "", known: str = "") -> tuple[Live | None, str]:
        result = known or video
        if not result:
            listing = await self.request("search", {"part": "snippet", "channelId": channel, "eventType": "live",
                                                   "type": "video", "maxResults": 5})
            if not listing["items"]:
                return None, "Offline"
            result = listing["items"][0]["id"]["videoId"]
        items = (await self.request("videos", {"part": "snippet,liveStreamingDetails", "id": result}))["items"]
        if not items:
            return None, "Video unavailable"
        item = items[0]
        details, snippet = item.get("liveStreamingDetails", {}), item["snippet"]
        if details.get("actualEndTime"):
            return None, "Broadcast ended"
        if not details.get("actualStartTime") or snippet.get("liveBroadcastContent") != "live":
            return None, "Scheduled" if details.get("scheduledStartTime") else "Offline"
        return Live(result, f"https://www.youtube.com/watch?v={result}", snippet["title"],
                    snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                    int(details["concurrentViewers"]) if details.get("concurrentViewers") else None,
                    details.get("activeLiveChatId", "")), "Live"

    async def chat(self, live: Live, message: ChatCallback, status: Callable[[str], None]) -> None:
        if not live.chat:
            raise PlatformError("This broadcast has no public live chat ID")
        token = self.cursors.get(live.chat, "")
        self.consume("youtube", 5)
        async with grpc.aio.secure_channel("youtube.googleapis.com:443", grpc.ssl_channel_credentials()) as channel:
            rpc = channel.unary_stream("/youtube.api.v3.V3DataLiveChatMessageService/StreamList",
                                       request_serializer=lambda req: req.SerializeToString(),
                                       response_deserializer=self.Response.FromString)
            req = self.Request(live_chat_id=live.chat, page_token=token, part=["id", "snippet"])
            connected = False
            try:
                async for response in rpc(req, metadata=tuple((key.lower(), value) for key, value in self.headers.items()), timeout=7200):
                    if not connected:
                        status("YouTube streamList connected")
                        connected = True
                    token = response.next_page_token or token
                    self._cursor(live.chat, token)
                    if response.offline_at:
                        raise PlatformError("YouTube live chat has ended")
                    for item in response.items:
                        snippet = item.snippet
                        if snippet.type in (1, 3, 15, 16, 17):
                            message(item.id, snippet.author_channel_id, snippet.display_message,
                                    snippet.type == 16, timestamp(snippet.published_at))
            except grpc.aio.AioRpcError as exc:
                if exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                    raise QuotaError("YouTube chat quota or rate limit reached") from None
                if exc.code() not in {grpc.StatusCode.UNIMPLEMENTED, grpc.StatusCode.UNAVAILABLE,
                                      grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.DEADLINE_EXCEEDED,
                                      grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED}:
                    raise PlatformError(f"YouTube chat stream unavailable ({exc.code().name})") from None
        status("Connecting to YouTube chat polling fallback")
        while True:
            params = {"part": "id,snippet", "liveChatId": live.chat, "maxResults": 2000}
            if token:
                params["pageToken"] = token
            data = await self.request("liveChat/messages", params)
            status("YouTube list connected")
            token = data.get("nextPageToken", token)
            self._cursor(live.chat, token)
            if data.get("offlineAt"):
                raise PlatformError("YouTube live chat has ended")
            for item in data["items"]:
                snippet = item["snippet"]
                if snippet["type"] in {"textMessageEvent", "fanFundingEvent", "superChatEvent",
                                       "superStickerEvent", "memberMilestoneChatEvent"}:
                    message(item["id"], snippet.get("authorChannelId", ""), snippet.get("displayMessage", ""),
                            snippet["type"] == "superStickerEvent", timestamp(snippet.get("publishedAt")))
            await asyncio.sleep(max(1, data["pollingIntervalMillis"] / 1000))


    def _cursor(self, chat: str, token: str) -> None:
        if token:
            if len(self.cursors) >= 64 and chat not in self.cursors:
                del self.cursors[next(iter(self.cursors))]
            self.cursors[chat] = token
