"""Durable free Chat allowance and separately revocable Chat-only access."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DAILY_LIMIT = 5
CHAT_COOKIE = "climate_chat_access"
INVALID_TOKEN = "This token is invalid or has been revoked."
NEW_YORK = ZoneInfo("America/New_York")


def calendar_window(now: datetime | None = None) -> tuple[str, str]:
    local = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), NEW_YORK)
    return local.date().isoformat(), midnight.isoformat()


def _verifier(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class ChatAccessStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS usage (
                    ip TEXT NOT NULL, day TEXT NOT NULL, completed INTEGER NOT NULL,
                    PRIMARY KEY (ip, day));
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, ip TEXT NOT NULL, day TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tokens (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, created_at TEXT NOT NULL,
                    verifier TEXT UNIQUE NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions (
                    verifier TEXT PRIMARY KEY, token_id TEXT NOT NULL);
            """)
            # The declared stack runs one Uvicorn process; a restart abandons its in-flight work.
            connection.execute("DELETE FROM reservations")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def _remaining(self, connection: sqlite3.Connection, ip: str, day: str) -> int:
        row = connection.execute("SELECT completed FROM usage WHERE ip = ? AND day = ?", (ip, day)).fetchone()
        pending = connection.execute("SELECT count(*) FROM reservations WHERE ip = ? AND day = ?", (ip, day)).fetchone()[0]
        return max(0, DAILY_LIMIT - (row[0] if row else 0) - pending)

    def status(self, ip: str, session: str | None = None, token: str | None = None) -> dict:
        day, reset_at = calendar_window()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            enabled = self._active_token(connection, session=session, token=token) is not None
            return {"remaining": self._remaining(connection, ip, day), "limit": DAILY_LIMIT,
                    "reset_at": reset_at, "timezone": "America/New_York", "access_enabled": enabled}

    def _active_token(self, connection: sqlite3.Connection, *, session: str | None = None, token: str | None = None) -> str | None:
        if token:
            row = connection.execute("SELECT id FROM tokens WHERE verifier = ? AND revoked = 0", (_verifier(token),)).fetchone()
        elif session:
            row = connection.execute("SELECT t.id FROM tokens t JOIN sessions s ON t.id = s.token_id WHERE s.verifier = ? AND t.revoked = 0", (_verifier(session),)).fetchone()
        else:
            row = None
        return row[0] if row else None

    def reserve(self, ip: str, *, session: str | None = None, token: str | None = None) -> str | None:
        day, _ = calendar_window()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if session or token:
                if self._active_token(connection, session=session, token=token) is None:
                    raise PermissionError(INVALID_TOKEN)
                return None
            if self._remaining(connection, ip, day) == 0:
                raise OverflowError("Free Chat allowance exhausted.")
            reservation = secrets.token_urlsafe(24)
            connection.execute("INSERT INTO reservations VALUES (?, ?, ?)", (reservation, ip, day))
            return reservation

    def finish(self, reservation: str | None, *, completed: bool) -> None:
        if reservation is None:
            return
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT ip, day FROM reservations WHERE id = ?", (reservation,)).fetchone()
            if row and completed:
                connection.execute("INSERT INTO usage VALUES (?, ?, 1) ON CONFLICT(ip, day) DO UPDATE SET completed = completed + 1", row)
            connection.execute("DELETE FROM reservations WHERE id = ?", (reservation,))

    def create_token(self, label: str) -> dict:
        secret = secrets.token_urlsafe(32)
        item = {"id": secrets.token_urlsafe(16), "label": label,
                "created_at": datetime.now(timezone.utc).isoformat(), "status": "active"}
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT INTO tokens(id, label, created_at, verifier) VALUES (?, ?, ?, ?)",
                               (item["id"], label, item["created_at"], _verifier(secret)))
        return {**item, "token": secret}

    def list_tokens(self) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT id, label, created_at, revoked FROM tokens ORDER BY created_at DESC").fetchall()
        return [{"id": row[0], "label": row[1], "created_at": row[2], "status": "revoked" if row[3] else "active"} for row in rows]

    def revoke_token(self, token_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            return connection.execute("UPDATE tokens SET revoked = 1 WHERE id = ?", (token_id,)).rowcount > 0

    def enable(self, token: str) -> str:
        session = secrets.token_urlsafe(32)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            token_id = self._active_token(connection, token=token)
            if token_id is None:
                raise PermissionError(INVALID_TOKEN)
            connection.execute("INSERT INTO sessions VALUES (?, ?)", (_verifier(session), token_id))
        return session
