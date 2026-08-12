import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import InputError


ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
RECIPIENT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    username: str
    password: str
    from_address: str
    security: str


@dataclass(frozen=True)
class Recipient:
    id: str
    address: str


@dataclass(frozen=True)
class DeliveryConfig:
    smtp: SMTPConfig
    recipients: tuple[Recipient, ...]


def _env_name(value: object, field: str) -> str:
    if not isinstance(value, str) or not ENV_NAME.fullmatch(value):
        raise InputError(f"{field} must name an environment variable")
    return value


def _resolved_env(mapping: dict, key: str) -> str:
    name = _env_name(mapping.get(key), f"smtp.{key}")
    value = os.environ.get(name, "")
    if not value:
        raise InputError(f"required environment variable {name} is not set")
    return value


def load_delivery_config(path: Path) -> DeliveryConfig:
    path = Path(path)
    if not path.is_file():
        raise InputError("delivery config file does not exist")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InputError("delivery config must be valid UTF-8 YAML") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise InputError("delivery config version must be 1")
    if set(payload) != {"version", "smtp", "recipients"}:
        raise InputError("delivery config contains unsupported keys")
    smtp = payload.get("smtp")
    recipients = payload.get("recipients")
    if not isinstance(smtp, dict) or not isinstance(recipients, list) or not recipients:
        raise InputError("delivery config requires smtp and recipients")
    expected_smtp = {
        "host_env",
        "port_env",
        "username_env",
        "password_env",
        "from_address_env",
        "security",
    }
    if set(smtp) != expected_smtp:
        raise InputError("smtp config contains missing or unsupported keys")
    if any(not isinstance(item, dict) or set(item) != {"id", "address"} for item in recipients):
        raise InputError("each recipient must contain exactly id and address keys")
    security = smtp.get("security")
    if security not in {"starttls", "ssl"}:
        raise InputError("smtp.security must be starttls or ssl")
    try:
        port = int(_resolved_env(smtp, "port_env"))
    except ValueError as exc:
        raise InputError("SMTP port environment variable must contain an integer") from exc
    if not 1 <= port <= 65535:
        raise InputError("SMTP port is out of range")
    from_address = _resolved_env(smtp, "from_address_env")
    if not EMAIL.fullmatch(from_address):
        raise InputError("SMTP from address is invalid")

    resolved: list[Recipient] = []
    seen: set[str] = set()
    seen_addresses: set[str] = set()
    for item in recipients:
        if not isinstance(item, dict) or not RECIPIENT_ID.fullmatch(str(item.get("id", ""))):
            raise InputError("each recipient requires a stable lowercase id")
        recipient_id = str(item["id"])
        if recipient_id in seen:
            raise InputError("recipient ids must be unique")
        seen.add(recipient_id)
        address = item.get("address", "").strip() if isinstance(item.get("address"), str) else ""
        if not isinstance(address, str) or not EMAIL.fullmatch(address):
            raise InputError(f"recipient {recipient_id} address is invalid")
        normalized_address = address.casefold()
        if normalized_address in seen_addresses:
            raise InputError("recipient addresses must be unique")
        seen_addresses.add(normalized_address)
        resolved.append(Recipient(recipient_id, address))

    return DeliveryConfig(
        smtp=SMTPConfig(
            host=_resolved_env(smtp, "host_env"),
            port=port,
            username=_resolved_env(smtp, "username_env"),
            password=_resolved_env(smtp, "password_env"),
            from_address=from_address,
            security=security,
        ),
        recipients=tuple(resolved),
    )
