"""Streamlink discovery and a read-only, anonymous Twitch IRC connection."""
import asyncio
import json
import re
from secrets import randbelow
from uuid import uuid4

from websockets.asyncio.client import connect

from .media import run
from .platforms import ChatCallback, Live, PlatformError


class AnonymousTwitch:
    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.url = f"https://www.twitch.tv/{channel}"

    async def probe(self, previous: Live | None = None) -> Live | None:
        raw = await run("streamlink", "--json", "--http-timeout", "8", self.url,
                        timeout=18, accepted_codes=(0, 1))
        try:
            result = json.loads(raw)
        except ValueError:
            raise PlatformError("Streamlink could not check the Twitch channel") from None
        if (result.get("error") or "").startswith("No playable streams found"):
            return None
        if not result.get("streams"):
            raise PlatformError("Streamlink could not find a playable Twitch playlist")
        metadata = result.get("metadata") or {}
        broadcast = metadata.get("id") or (previous.broadcast if previous else uuid4().hex)
        return Live(str(broadcast), self.url, metadata.get("title") or self.channel)

    async def chat(self, _live: Live, message: ChatCallback, status) -> None:
        nick = f"justinfan{10000 + randbelow(900000)}"
        async with connect("wss://irc-ws.chat.twitch.tv:443", open_timeout=10, close_timeout=3) as ws:
            await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await ws.send(f"NICK {nick}")
            await ws.send(f"JOIN #{self.channel}")
            async with asyncio.timeout(15):
                while not await self.receive(ws, await ws.recv(), nick, message):
                    pass
            status("Anonymous Twitch chat connected")
            async for frame in ws:
                await self.receive(ws, frame, nick, message)

    async def receive(self, ws, frame: str, nick: str, message: ChatCallback) -> bool:
        joined = False
        for line in frame.splitlines():
            if line.startswith("PING "):
                await ws.send("PONG " + line[5:])
                continue
            if re.match(r"^:\S+ (?:NOTICE \*|RECONNECT)(?: |$)", line):
                raise PlatformError("Twitch closed or rejected anonymous chat")
            if re.match(rf"^:{nick}!\S+ JOIN #{self.channel}(?: |$)", line) or (
                f" 366 {nick} #{self.channel} :" in line or f" ROOMSTATE #{self.channel}" in line
            ):
                joined = True
            chat = re.fullmatch(r"(?:@(\S+) )?:(\w+)!\S+ PRIVMSG #(\w+) :(.*)", line)
            if chat and chat[3].lower() == self.channel:
                joined = True
                tags = dict(item.split("=", 1) for item in (chat[1] or "").split(";") if "=" in item)
                posted = tags.get("tmi-sent-ts", "")
                message(tags.get("id", ""), tags.get("user-id", chat[2]), chat[4],
                        bool(tags.get("emotes")), int(posted) / 1000 if posted.isascii() and posted.isdigit() else None)
        return joined
