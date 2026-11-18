import json
import os
import re
import sqlite3
import time
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from .platforms import QuotaError


class Settings(BaseModel):
    slots: int = Field(3, ge=1, le=3)
    buffer: int = Field(100, ge=90, le=600)
    pre: int = Field(20, ge=0, le=60)
    post: int = Field(20, ge=2, le=90)
    maximum: int = Field(120, ge=15, le=300)
    quality: str = Field("best", pattern=r"^(best|worst|1080p|720p|480p|360p)$")
    preset: str = Field("Balanced", pattern=r"^(Low|Balanced|High|Manual)$")
    rise: float = Field(2.5, ge=1.2, le=10)
    deviations: float = Field(5, ge=2, le=20)
    baseline: int = Field(120, ge=30, le=900)
    onset: int = Field(2, ge=1, le=12)
    cooldown: int = Field(35, ge=6, le=600)
    min_users: int = Field(5, ge=2, le=500)
    per_hour: int = Field(12, ge=1, le=60)
    disk_gb: float = Field(20, ge=0.25, le=10000)
    notification: bool = True
    nvenc: bool = False
    output: str = ""
    youtube_search_budget: int = Field(90, ge=1, le=50000)
    youtube_units: int = Field(8500, ge=20, le=10000000)

    @model_validator(mode="after")
    def duration(self) -> "Settings":
        if self.maximum < self.pre + self.post + 5:
            raise ValueError("Maximum clip duration must exceed pre-roll plus post-roll by five seconds")
        if self.buffer < self.pre + 40:
            raise ValueError("The buffer must cover pre-roll plus 40 seconds of timing adjustment")
        if self.output and not Path(self.output).expanduser().is_absolute():
            raise ValueError("Use an absolute output directory")
        return self


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "blynx.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=3000;
            CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS channel (
              id TEXT PRIMARY KEY, platform TEXT NOT NULL, remote TEXT NOT NULL, url TEXT NOT NULL,
              name TEXT NOT NULL, avatar TEXT NOT NULL, video TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
              priority INTEGER NOT NULL DEFAULT 50, offset REAL NOT NULL DEFAULT 0, preset TEXT NOT NULL DEFAULT '',
              UNIQUE(platform, remote, video)
            );
            CREATE TABLE IF NOT EXISTS clip (
              id TEXT PRIMARY KEY, channel TEXT NOT NULL, name TEXT NOT NULL, platform TEXT NOT NULL,
              broadcast TEXT NOT NULL, source TEXT NOT NULL, onset REAL NOT NULL,
              score REAL NOT NULL, offset REAL NOT NULL, reason TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL, review TEXT NOT NULL DEFAULT 'New', favorite INTEGER NOT NULL DEFAULT 0,
              path TEXT NOT NULL DEFAULT '', duration REAL NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
              partial INTEGER NOT NULL DEFAULT 0, thumbnail INTEGER NOT NULL DEFAULT 0,
              provisional INTEGER NOT NULL DEFAULT 0, simulated INTEGER NOT NULL DEFAULT 0,
              journal TEXT NOT NULL DEFAULT '{}',
              UNIQUE(channel, broadcast, onset)
            );
            CREATE TABLE IF NOT EXISTS session (
              id TEXT PRIMARY KEY, channel TEXT NOT NULL, platform TEXT NOT NULL, started REAL NOT NULL,
              ended REAL NOT NULL, outcome TEXT NOT NULL, simulated INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cut_clip (
              id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
              channel TEXT NOT NULL, preset TEXT NOT NULL, created REAL NOT NULL,
              duration REAL, bytes INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cut_edit (
              cut_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
              recipe TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quota (
              day TEXT NOT NULL, bucket TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(day,bucket)
            );
        """)
        if "score_version" not in {row["name"] for row in self.db.execute("PRAGMA table_info(clip)")}:
            self.db.execute("ALTER TABLE clip ADD COLUMN score_version INTEGER NOT NULL DEFAULT 1")
        server_defaults = {"slots": 1, "notification": False, "quality": "720p"} if (
            os.getenv("BLYNX_SERVER_MODE") == "1"
        ) else {}
        self.settings = Settings.model_validate(self.config("settings", server_defaults))
        self.started = bool(self.config("running", True))
        self.db.execute("UPDATE session SET outcome='interrupted' WHERE outcome='active'")
        self.db.commit()

    def import_cut_clips(self) -> None:
        originals = {Path(row["path"]): row for row in self.all(
            "SELECT path,name,channel FROM clip WHERE path<>''")}
        directories = {self.root / "clips", self.clips_dir(), *(path.parent for path in originals)}
        known = {Path(row["path"]) for row in self.all("SELECT path FROM cut_clip")}
        recovered = []
        for directory in directories:
            for path in (directory / "exports").glob("*.mp4"):
                match = re.fullmatch(r"(.+)_(original|vertical)_(?:[a-f0-9]{6}|[a-f0-9]{32})\.mp4", path.name)
                if not match or path in known:
                    continue
                try:
                    if not path.is_file() or path.is_symlink():
                        continue
                    info = path.stat()
                except OSError:
                    continue
                if not info.st_size:
                    continue
                stem, preset = match.groups()
                original = originals.get(directory / f"{stem}.mp4")
                recovered.append((sha256(os.fsencode(path)).hexdigest(), str(path),
                                  original["name"] if original else stem,
                                  original["channel"] if original else "", preset,
                                  info.st_mtime, None, info.st_size))
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO cut_clip VALUES(?,?,?,?,?,?,?,?)", recovered)

    def config(self, key: str, default):
        row = self.db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        self.execute("INSERT OR REPLACE INTO config VALUES(?,?)", (key, json.dumps(value)))

    def execute(self, sql: str, args: tuple = ()) -> None:
        with self.db:
            self.db.execute(sql, args)

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, args)]

    def item(self, sql: str, args: tuple = ()) -> dict | None:
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def save_settings(self, settings: Settings) -> None:
        self.set("settings", settings.model_dump())
        self.settings = settings

    def quota(self, bucket: str, amount: int) -> None:
        day = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
        limit = self.settings.youtube_search_budget if bucket == "search" else self.settings.youtube_units
        record = self.item("SELECT count FROM quota WHERE day=? AND bucket=?", (day, bucket))
        count = record["count"] if record else 0
        if count + amount > limit:
            raise QuotaError(f"YouTube {bucket} daily budget reached")
        self.execute("INSERT OR REPLACE INTO quota VALUES(?,?,?)", (day, bucket, count + amount))

    def clips_dir(self) -> Path:
        if self.settings.output:
            return Path(self.settings.output).expanduser()
        return self.root / "clips"

    def stop_recent_session(self, key: str, outcome: str) -> None:
        self.execute("UPDATE session SET ended=?,outcome=? WHERE id=?", (time.time(), outcome, key))

    @classmethod
    def default(cls) -> "Store":
        location = os.getenv("BLYNX_DATA_DIR")
        default = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "blynx-clips"
        return cls(Path(location).expanduser() if location else default)

