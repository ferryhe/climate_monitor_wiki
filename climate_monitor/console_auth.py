"""FastAPI Users integration for the single-operator management console.

The deployment supplies an Argon2 password hash, never a plaintext password.
FastAPI Users owns password verification, JWT creation/validation, and the
HTTP-only cookie transport.  The read-only user adapter intentionally exposes
no registration or account-management surface.
"""
from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass
from typing import AsyncGenerator

from fastapi_users import BaseUserManager, FastAPIUsers, InvalidID
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import BaseUserDatabase


_USER_NAMESPACE = uuid.UUID("6d53b4b7-bb71-4a9b-bfc5-85dad47cfb72")
_EPHEMERAL_SESSION_SECRET = secrets.token_urlsafe(48)


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


cookie_transport = CookieTransport(
    cookie_name="climate_console_session",
    cookie_max_age=_session_seconds(),
    cookie_secure=os.getenv("CLIMATE_CONSOLE_SECURE_COOKIE", "").lower() in {"1", "true", "yes"},
    cookie_httponly=True,
    cookie_samesite="strict",
)


def get_jwt_strategy() -> JWTStrategy:
    secret = os.getenv("CLIMATE_CONSOLE_SESSION_SECRET", "").strip()
    # A per-process fallback permits unauthenticated health endpoints without
    # making an absent deployment secret forgeable. Login remains disabled.
    return JWTStrategy(secret=secret or _EPHEMERAL_SESSION_SECRET, lifetime_seconds=_session_seconds())


auth_backend = AuthenticationBackend(
    name="console-cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)
fastapi_users = FastAPIUsers[ConsoleUser, uuid.UUID](get_user_manager, [auth_backend])
current_console_user = fastapi_users.current_user(active=True, verified=True)
optional_console_user = fastapi_users.current_user(optional=True, active=True, verified=True)
auth_router = fastapi_users.get_auth_router(auth_backend, requires_verification=True)
