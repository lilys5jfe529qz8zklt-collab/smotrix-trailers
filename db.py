from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from config import DATA_DIR


class DB:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (DATA_DIR / "trailers.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channels (
                url TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_polled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS trailers (
                youtube_id TEXT PRIMARY KEY,
                title_id INTEGER NOT NULL DEFAULT 0,
                title_name TEXT NOT NULL DEFAULT '',
                title_year INTEGER NOT NULL DEFAULT 0,
                youtube_title TEXT NOT NULL DEFAULT '',
                published_at INTEGER NOT NULL DEFAULT 0,
                quality INTEGER NOT NULL DEFAULT 1080,
                height INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                message_id INTEGER NOT NULL DEFAULT 0,
                chat_id INTEGER NOT NULL DEFAULT 0,
                size INTEGER NOT NULL DEFAULT 0,
                duration INTEGER NOT NULL DEFAULT 0,
                width INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS trailers_title ON trailers(title_id, status);
            CREATE INDEX IF NOT EXISTS trailers_published ON trailers(published_at);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def quality(self) -> int:
        try:
            return int(self.setting("quality", "1080"))
        except ValueError:
            return 1080

    def set_quality(self, value: int) -> None:
        self.set_setting("quality", str(int(value)))

    def add_channel(self, url: str, channel_id: str, title: str) -> None:
        self.conn.execute(
            "INSERT INTO channels(url,channel_id,title) VALUES(?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET channel_id=excluded.channel_id, title=excluded.title",
            (url, channel_id, title),
        )
        self.conn.commit()

    def channels(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM channels"
        if enabled_only:
            sql += " WHERE enabled=1"
        return list(self.conn.execute(sql + " ORDER BY url").fetchall())

    def set_channel_polled(self, url: str) -> None:
        self.conn.execute("UPDATE channels SET last_polled=? WHERE url=?", (int(time.time()), url))
        self.conn.commit()

    def seen(self, youtube_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM trailers WHERE youtube_id=?", (youtube_id,)
        ).fetchone()

    def add_trailer(self, data: dict[str, Any], status: str = "pending") -> bool:
        now = int(time.time())
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO trailers(
                youtube_id,title_id,title_name,title_year,youtube_title,published_at,
                quality,status,error,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data["youtube_id"], int(data.get("title_id") or 0),
                data.get("title_name") or "", int(data.get("title_year") or 0),
                data.get("youtube_title") or "", int(data.get("published_at") or 0),
                int(data.get("quality") or self.quality()), status,
                data.get("error") or "", now, now,
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_status(self, youtube_id: str, status: str, error: str = "", **fields: Any) -> None:
        parts = ["status=?", "error=?", "updated_at=?"]
        values: list[Any] = [status, str(error)[:500], int(time.time())]
        for key, value in fields.items():
            parts.append(f"{key}=?")
            values.append(value)
        values.append(youtube_id)
        self.conn.execute(f"UPDATE trailers SET {', '.join(parts)} WHERE youtube_id=?", values)
        self.conn.commit()

    def bump_attempt(self, youtube_id: str) -> int:
        self.conn.execute(
            "UPDATE trailers SET attempts=attempts+1, updated_at=? WHERE youtube_id=?",
            (int(time.time()), youtube_id),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT attempts FROM trailers WHERE youtube_id=?", (youtube_id,)
        ).fetchone()
        return int(row["attempts"]) if row else 0

    def next_pending(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM trailers WHERE status='pending' ORDER BY published_at DESC, created_at LIMIT 1"
        ).fetchone()

    def retry(self, youtube_id: str) -> None:
        self.conn.execute(
            "UPDATE trailers SET status='pending', error='', attempts=0, updated_at=? WHERE youtube_id=?",
            (int(time.time()), youtube_id),
        )
        self.conn.commit()

    def latest(self, title_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM trailers WHERE title_id=? AND status='done' AND message_id>0 "
            "ORDER BY published_at DESC, updated_at DESC LIMIT 1",
            (int(title_id),),
        ).fetchone()

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM trailers WHERE status='done' AND message_id>0 "
                "ORDER BY published_at DESC LIMIT ?",
                (max(1, min(50, int(limit))),),
            ).fetchall()
        )

    def list(self, limit: int = 60) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM trailers ORDER BY created_at DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
        )

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM trailers GROUP BY status"
        ).fetchall()
        out = {str(row["status"]): int(row["n"]) for row in rows}
        out["bytes"] = int(
            self.conn.execute(
                "SELECT COALESCE(SUM(size),0) AS n FROM trailers WHERE status='done'"
            ).fetchone()["n"]
        )
        return out

    @staticmethod
    def as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def dump_json(self, row: sqlite3.Row | None) -> str:
        return json.dumps(self.as_dict(row), ensure_ascii=False)
