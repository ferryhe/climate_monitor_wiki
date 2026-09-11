"""FastAPI Users integration for the single-operator management console.

The deployment supplies an Argon2 password hash, never a plaintext password.
FastAPI Users owns password verification, JWT creation/validation, and the
HTTP-only cookie transport.  The read-only user adapter intentionally exposes
no registration or account-management surface.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator

from fastapi_users import BaseUserManager, FastAPIUsers, InvalidID
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import BaseUserDatabase
from fastapi_users.jwt import decode_jwt, generate_jwt


_USER_NAMESPACE = uuid.UUID("6d53b4b7-bb71-4a9b-bfc5-85dad47cfb72")
_EPHEMERAL_SESSION_SECRET = secrets.token_urlsafe(48)
_DEFAULT_SESSION_DB = Path(tempfile.gettempdir()) / f"climate-console-sessions-{os.getpid()}.sqlite3"
_INITIALIZED_SESSION_DATABASES: set[Path] = set()
_SESSION_DATABASE_INIT_LOCK = threading.Lock()


@dataclass
class ConsoleUser:
    id: uuid.UUID
    email: str
    hashed_password: str
    is_active: bool = True
    is_superuser: bool = True
    is_verified: bool = True


def _configured_user() -> ConsoleUser | None:
    username = os.getenv("CLIMATE_CONSOLE_USERNAME", "").strip()
    password_hash = os.getenv("CLIMATE_CONSOLE_PASSWORD_HASH", "").strip()
    session_secret = os.getenv("CLIMATE_CONSOLE_SESSION_SECRET", "")
    if not username or not password_hash.startswith("$argon2") or len(session_secret) < 32:
        return None
    return ConsoleUser(
        id=uuid.uuid5(_USER_NAMESPACE, username),
        email=username,
        hashed_password=password_hash,
    )


class EnvironmentUserDatabase(BaseUserDatabase[ConsoleUser, uuid.UUID]):
    """Read-only adapter for one externally provisioned operator account."""

    async def get(self, id: uuid.UUID) -> ConsoleUser | None:
        user = _configured_user()
        return user if user is not None and user.id == id else None

    async def get_by_email(self, email: str) -> ConsoleUser | None:
        user = _configured_user()
        return user if user is not None and user.email == email else None

    async def get_by_oauth_account(self, oauth: str, account_id: str) -> ConsoleUser | None:
        return None

    async def create(self, create_dict: dict) -> ConsoleUser:
        raise RuntimeError("console self-registration is disabled")

    async def update(self, user: ConsoleUser, update_dict: dict) -> ConsoleUser:
        # FastAPI Users only calls this to upgrade a legacy password hash.  The
        # deployment is intentionally immutable, so reject unexpected changes
        # rather than pretending an environment credential was persisted.
        if update_dict.get("hashed_password") == user.hashed_password:
            return user
        raise RuntimeError("replace CLIMATE_CONSOLE_PASSWORD_HASH and restart to rotate credentials")

    async def delete(self, user: ConsoleUser) -> None:
        raise RuntimeError("console account deletion is managed by deployment configuration")

_USER_DATABASE = EnvironmentUserDatabase()


async def get_user_database() -> AsyncGenerator[EnvironmentUserDatabase, None]:
    yield _USER_DATABASE


class ConsoleUserManager(BaseUserManager[ConsoleUser, uuid.UUID]):
    reset_password_token_secret = "unused-registration-disabled"
    verification_token_secret = "unused-registration-disabled"

    def parse_id(self, value: str) -> uuid.UUID:
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise InvalidID() from exc


async def get_user_manager() -> AsyncGenerator[ConsoleUserManager, None]:
    yield ConsoleUserManager(_USER_DATABASE)


def _session_seconds() -> int:
    return int(os.getenv("CLIMATE_CONSOLE_SESSION_SECONDS", "1800"))


def _session_db_path() -> Path:
    configured = os.getenv("CLIMATE_CONSOLE_SESSION_DB", "").strip()
    return Path(configured).expanduser() if configured else _DEFAULT_SESSION_DB


class ConsoleSessionStore:
    """Deployment-shared allowlist for independently revocable login sessions."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _session_db_path()

        database = self.path.resolve(strict=False)
        if database in _INITIALIZED_SESSION_DATABASES:
            return
        with _SESSION_DATABASE_INIT_LOCK:
            if database in _INITIALIZED_SESSION_DATABASES:
                return
            database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            connection = sqlite3.connect(database, timeout=5)
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS console_sessions (
                        session_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        expires_at INTEGER NOT NULL
                    )"""
                )
                connection.commit()
            finally:
                connection.close()
            try:
                database.chmod(0o600)
            except OSError:
                pass
            _INITIALIZED_SESSION_DATABASES.add(database)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        return connection

    def create(self, session_id: str, user_id: str, expires_at: int) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("DELETE FROM console_sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO console_sessions(session_id, user_id, expires_at) VALUES (?, ?, ?)",
                (session_id, user_id, expires_at),
            )

    def is_active(self, session_id: str, user_id: str) -> bool:
        now = int(time.time())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM console_sessions WHERE session_id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if row is not None and int(row[0]) <= now:
                connection.execute(
                    "DELETE FROM console_sessions WHERE session_id = ?", (session_id,)
                )
                return False
        return row is not None

    def revoke(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM console_sessions WHERE session_id = ?", (session_id,)
            )


class ConsoleSessionJWTStrategy(JWTStrategy[ConsoleUser, uuid.UUID]):
    """JWT strategy with a random login identity backed by durable state."""

    def __init__(self, *, secret: str, lifetime_seconds: int) -> None:
        super().__init__(secret=secret, lifetime_seconds=lifetime_seconds)
        self.sessions = ConsoleSessionStore()

    def _decode(self, token: str) -> dict | None:
        try:
            return decode_jwt(
                token,
                self.decode_key,
                self.token_audience,
                algorithms=[self.algorithm],
            )
        except Exception:
            return None

    async def write_token(self, user: ConsoleUser) -> str:
        session_id = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + max(int(self.lifetime_seconds or 0), 1)
        self.sessions.create(session_id, str(user.id), expires_at)
        return generate_jwt(
            {"sub": str(user.id), "aud": self.token_audience, "jti": session_id},
            self.encode_key,
            self.lifetime_seconds,
            algorithm=self.algorithm,
        )

    async def read_token(
        self, token: str | None, user_manager: BaseUserManager[ConsoleUser, uuid.UUID]
    ) -> ConsoleUser | None:
        if token is None:
            return None
        data = self._decode(token)
        if data is None:
            return None
        user_id = data.get("sub")
        session_id = data.get("jti")
        if not isinstance(user_id, str) or not isinstance(session_id, str):
            return None
        try:
            if not self.sessions.is_active(session_id, user_id):
                return None
        except (OSError, sqlite3.Error):
            return None
        try:
            return await super().read_token(token, user_manager)
        except (OSError, sqlite3.Error):
            return None

    async def destroy_token(self, token: str, user: ConsoleUser) -> None:
        data = self._decode(token)
        session_id = data.get("jti") if data else None
        if isinstance(session_id, str):
            self.sessions.revoke(session_id)


cookie_transport = CookieTransport(
    cookie_name="climate_console_session",
    cookie_max_age=_session_seconds(),
    cookie_secure=os.getenv("CLIMATE_CONSOLE_SECURE_COOKIE", "").lower() in {"1", "true", "yes"},
    cookie_httponly=True,
    cookie_samesite="strict",
)


def get_jwt_strategy() -> ConsoleSessionJWTStrategy:
    secret = os.getenv("CLIMATE_CONSOLE_SESSION_SECRET", "").strip()
    # A per-process fallback permits unauthenticated health endpoints without
    # making an absent deployment secret forgeable. Login remains disabled.
    return ConsoleSessionJWTStrategy(
        secret=secret or _EPHEMERAL_SESSION_SECRET,
        lifetime_seconds=_session_seconds(),
    )


auth_backend = AuthenticationBackend(
    name="console-cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)
fastapi_users = FastAPIUsers[ConsoleUser, uuid.UUID](get_user_manager, [auth_backend])
current_console_user = fastapi_users.current_user(active=True, verified=True)
optional_console_user = fastapi_users.current_user(optional=True, active=True, verified=True)
auth_router = fastapi_users.get_auth_router(auth_backend, requires_verification=True)
