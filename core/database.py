import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(slots=True)
class CookieRecord:
    id: int
    nickname: str
    cookie: str
    created_at: str


@dataclass(slots=True)
class ServerRecord:
    id: int
    name: str
    link: str
    created_at: str


class Database:
    def __init__(self, db_path: str = "storage.db", server_json_path: str = "server.json") -> None:
        self.db_path = Path(db_path)
        self.server_json_path = Path(server_json_path)
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._execute_script(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;

            CREATE TABLE IF NOT EXISTS cookies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname TEXT NOT NULL UNIQUE,
                cookie TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                link TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        await self.sync_servers_to_json()

    async def _execute_script(self, script: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._execute_script_sync, script)

    def _execute_script_sync(self, script: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(script)
            conn.commit()

    async def _execute(
        self,
        query: str,
        params: tuple[Any, ...] = (),
        *,
        fetchone: bool = False,
        fetchall: bool = False,
    ) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._execute_sync, query, params, fetchone, fetchall)

    def _execute_sync(
        self,
        query: str,
        params: tuple[Any, ...],
        fetchone: bool,
        fetchall: bool,
    ) -> Any:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query, params)
            result = None
            if fetchone:
                row = cur.fetchone()
                result = dict(row) if row else None
            elif fetchall:
                rows = cur.fetchall()
                result = [dict(row) for row in rows]
            conn.commit()
            return result

    # ----------------------------- Cookies -----------------------------
    async def add_or_replace_cookie(self, nickname: str, cookie: str) -> None:
        await self._execute(
            """
            INSERT INTO cookies (nickname, cookie)
            VALUES (?, ?)
            ON CONFLICT(nickname) DO UPDATE SET cookie = excluded.cookie
            """,
            (nickname.strip(), cookie.strip()),
        )

    async def delete_cookie(self, nickname: str) -> bool:
        before = await self.get_cookie(nickname)
        if not before:
            return False
        await self._execute("DELETE FROM cookies WHERE nickname = ?", (nickname.strip(),))
        return True

    async def get_cookie(self, nickname: str) -> Optional[CookieRecord]:
        row = await self._execute(
            "SELECT id, nickname, cookie, created_at FROM cookies WHERE nickname = ?",
            (nickname.strip(),),
            fetchone=True,
        )
        if not row:
            return None
        return CookieRecord(**row)

    async def list_cookies(self) -> list[CookieRecord]:
        rows = await self._execute(
            "SELECT id, nickname, cookie, created_at FROM cookies ORDER BY id ASC",
            fetchall=True,
        )
        return [CookieRecord(**row) for row in rows]

    # ----------------------------- Servers -----------------------------
    async def add_or_replace_server(self, name: str, link: str) -> None:
        await self._execute(
            """
            INSERT INTO servers (name, link)
            VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET link = excluded.link
            """,
            (name.strip(), link.strip()),
        )
        await self.sync_servers_to_json()

    async def delete_server(self, name: str) -> bool:
        before = await self.get_server(name)
        if not before:
            return False
        await self._execute("DELETE FROM servers WHERE name = ?", (name.strip(),))
        await self.sync_servers_to_json()
        return True

    async def list_servers(self) -> list[ServerRecord]:
        rows = await self._execute(
            "SELECT id, name, link, created_at FROM servers ORDER BY id ASC",
            fetchall=True,
        )
        return [ServerRecord(**row) for row in rows]

    async def get_server(self, name: str) -> Optional[ServerRecord]:
        row = await self._execute(
            "SELECT id, name, link, created_at FROM servers WHERE name = ?",
            (name.strip(),),
            fetchone=True,
        )
        if not row:
            return None
        return ServerRecord(**row)

    # ----------------------------- Settings -----------------------------
    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key.strip(), value),
        )

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = await self._execute(
            "SELECT value FROM settings WHERE key = ?",
            (key.strip(),),
            fetchone=True,
        )
        if not row:
            return default
        return row["value"]

    async def sync_servers_to_json(self) -> None:
        servers = await self.list_servers()
        payload = [{"id": s.id, "name": s.name, "link": s.link, "created_at": s.created_at} for s in servers]
        self.server_json_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.server_json_path.write_text, json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
