from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import stat
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import idna

from climate_monitor.dedupe import canonical_title, canonical_url

from .classification import classify_document
from .contract import SchemaContractError, validate_registry_contract
from .errors import RegistryBuildError, RegistryInputError
from .reports import ParsedReport, parse_historical_report, parse_report_directory


INPUT_SCHEMA_VERSION = "registry-selection-input.v1"
OUTPUT_SCHEMA_VERSION = "registry-selection-plan.v1"
MAX_INPUT_BYTES = 1024 * 1024
MAX_CANDIDATES = 500
MAX_CANDIDATE_ID = 64
MAX_TITLE = 500
MAX_SUMMARY = 4_000
MAX_URL = 2_048
CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ROOT_KEYS = frozenset({"schema_version", "report_date", "candidates"})
CANDIDATE_KEYS = frozenset({"candidate_id", "pillar", "title", "summary", "url"})
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
PILLAR_ITEM = re.compile(r"^\s*-\s+\*\*(.+?)\*\*(?:\s*\([^)]*\))?\s*$")
BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
PERCENT_TRIPLET = re.compile(r"%([0-9A-Fa-f]{2})")
BAD_URI_CHARACTER = re.compile(r'[\\|<>"{}^`]')
DNS_LABEL = re.compile(r"^[A-Za-z0-9-]+$")
NUMERIC_HOST = re.compile(
    r"^(?:0[xX][0-9A-Fa-f]+|[0-9]+)(?:\.(?:0[xX][0-9A-Fa-f]+|[0-9]+)){0,3}$"
)
IPVFUTURE = re.compile(r"^v[0-9A-Fa-f]+\.[A-Za-z0-9._~!$&'()*+,;=:-]+$")
ASCII_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-"
)
SOURCE_MARKER = "🔗"


@dataclass(frozen=True)
class SelectionCandidate:
    candidate_id: str
    pillar: str
    title: str
    summary: str
    url: str


@dataclass(frozen=True)
class RegistrySelectionSnapshot:
    canonical_urls: frozenset[str]


def _reject_constant(_value: str) -> None:
    raise RegistryInputError("selection input contains a non-finite number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryInputError("selection input contains duplicate object keys")
        result[key] = value
    return result


def _string(value: Any, *, field: str, maximum: int, empty: bool = True) -> str:
    if not isinstance(value, str):
        raise RegistryInputError(f"selection candidate {field} must be a string")
    if len(value) > maximum:
        raise RegistryInputError(f"selection candidate {field} exceeds its size limit")
    if not empty and not value.strip():
        raise RegistryInputError(f"selection candidate {field} must not be empty")
    return value


def _validate_public_http_url(url: str) -> None:
    if (
        not url.isascii()
        or url != url.strip()
        or any(character.isspace() or unicodedata.category(character).startswith("C") for character in url)
        or BAD_PERCENT_ESCAPE.search(url)
        or BAD_URI_CHARACTER.search(url)
    ):
        raise RegistryInputError("selection candidate URL is invalid")
    for match in PERCENT_TRIPLET.finditer(url):
        token = match.group(1)
        if token != token.upper() or chr(int(token, 16)) in ASCII_UNRESERVED:
            raise RegistryInputError("selection candidate URL is invalid")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RegistryInputError("selection candidate URL is invalid") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.netloc.endswith(":")
        or (parsed.scheme.casefold() == "http" and port == 80)
        or (parsed.scheme.casefold() == "https" and port == 443)
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
        or any("[" in component or "]" in component for component in (parsed.path, parsed.query, parsed.fragment))
    ):
        raise RegistryInputError("selection candidate URL is invalid")

    bracketed = parsed.netloc.startswith("[")
    if bracketed:
        closing = parsed.netloc.find("]")
        literal = parsed.netloc[1:closing]
        suffix = parsed.netloc[closing + 1 :]
        if closing < 2 or (suffix and not suffix.startswith(":")) or "%" in literal:
            raise RegistryInputError("selection candidate URL is invalid")
        port_token = suffix[1:] if suffix else None
    else:
        port_token = (
            parsed.netloc.rpartition(":")[2] if ":" in parsed.netloc else None
        )
    if port_token is not None and (port is None or port_token != str(port)):
        raise RegistryInputError("selection candidate URL is invalid")

    if bracketed:
        if literal.startswith("v"):
            if not IPVFUTURE.fullmatch(literal):
                raise RegistryInputError("selection candidate URL is invalid")
        else:
            try:
                address = ipaddress.IPv6Address(literal)
            except ipaddress.AddressValueError as exc:
                raise RegistryInputError("selection candidate URL is invalid") from exc
            if literal != address.compressed:
                raise RegistryInputError("selection candidate URL is invalid")
        return

    if "%" in hostname or hostname.endswith(".") or len(hostname) > 253:
        raise RegistryInputError("selection candidate URL is invalid")
    if NUMERIC_HOST.fullmatch(hostname):
        try:
            address = ipaddress.IPv4Address(hostname)
        except ipaddress.AddressValueError as exc:
            raise RegistryInputError("selection candidate URL is invalid") from exc
        if hostname != str(address):
            raise RegistryInputError("selection candidate URL is invalid")
    labels = hostname.split(".")
    if any(
        not label
        or len(label) > 63
        or not DNS_LABEL.fullmatch(label)
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        raise RegistryInputError("selection candidate URL is invalid")
    for label in labels:
        if not label.casefold().startswith("xn--"):
            continue
        canonical_label = label.casefold()
        try:
            decoded = idna.decode(
                canonical_label, strict=True, uts46=False, std3_rules=True
            )
            roundtrip = idna.encode(
                decoded, strict=True, uts46=False, std3_rules=True
            ).decode("ascii")
        except idna.IDNAError as exc:
            raise RegistryInputError("selection candidate URL is invalid") from exc
        if roundtrip != canonical_label:
            raise RegistryInputError("selection candidate URL is invalid")


def _validate_payload(payload: Any) -> tuple[str, tuple[SelectionCandidate, ...]]:
    if not isinstance(payload, dict) or set(payload) != ROOT_KEYS:
        raise RegistryInputError("selection input has an invalid top-level contract")
    if payload.get("schema_version") != INPUT_SCHEMA_VERSION:
        raise RegistryInputError("selection input schema is unsupported")

    report_date = payload.get("report_date")
    if not isinstance(report_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        raise RegistryInputError("selection report_date is invalid")
    try:
        parsed_date = date.fromisoformat(report_date)
    except ValueError as exc:
        raise RegistryInputError("selection report_date is invalid") from exc
    if parsed_date.weekday() != 0:
        raise RegistryInputError("selection report_date must be a Monday")

    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise RegistryInputError("selection candidates must be an array")
    if len(raw_candidates) > MAX_CANDIDATES:
        raise RegistryInputError("selection candidate count exceeds its limit")

    candidates: list[SelectionCandidate] = []
    identifiers: set[str] = set()
    for raw in raw_candidates:
        if not isinstance(raw, dict) or set(raw) != CANDIDATE_KEYS:
            raise RegistryInputError("selection candidate has an invalid contract")
        candidate_id = _string(
            raw.get("candidate_id"), field="candidate_id", maximum=MAX_CANDIDATE_ID, empty=False
        )
        if not CANDIDATE_ID.fullmatch(candidate_id) or candidate_id in identifiers:
            raise RegistryInputError("selection candidate_id is invalid or duplicated")
        identifiers.add(candidate_id)
        pillar = raw.get("pillar")
        if pillar not in {"A", "B"}:
            raise RegistryInputError("selection candidate pillar must be A or B")
        title = _string(raw.get("title"), field="title", maximum=MAX_TITLE, empty=False)
        summary = _string(raw.get("summary"), field="summary", maximum=MAX_SUMMARY)
        url = _string(raw.get("url"), field="url", maximum=MAX_URL, empty=False)
        _validate_public_http_url(url)
        candidates.append(SelectionCandidate(candidate_id, pillar, title, summary, url))
    try:
        encoded_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, UnicodeEncodeError) as exc:
        raise RegistryInputError("selection input contains invalid string data") from exc
    if encoded_size > MAX_INPUT_BYTES:
        raise RegistryInputError("selection input exceeds the 1 MiB size limit")
    return report_date, tuple(candidates)


def load_selection_input(path: Path) -> dict[str, Any]:
    """Load and strictly validate a bounded selection input document."""

    input_path = Path(path)
    try:
        metadata = input_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RegistryInputError("selection input is not a readable file") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RegistryInputError("selection input must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(input_path, flags)
    except OSError as exc:
        raise RegistryInputError("selection input is not a readable regular file") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise RegistryInputError("selection input must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise RegistryInputError("selection input is not a readable regular file") from exc
    finally:
        os.close(descriptor)
    if len(raw) > MAX_INPUT_BYTES:
        raise RegistryInputError("selection input exceeds the 1 MiB size limit")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _validate_payload(payload)
    except RegistryInputError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise RegistryInputError("selection input is not valid bounded JSON") from exc
    return payload


def _pillar_section(text: str, pillar: str) -> str:
    headings = list(HEADING.finditer(text))
    matches = [
        heading
        for heading in headings
        if len(heading.group(1)) == 2
        and f"pillar {pillar.casefold()}" in heading.group(2).casefold()
    ]
    if len(matches) != 1:
        raise RegistryInputError("weekly report Pillar sections are invalid")
    selected = matches[0]
    end = len(text)
    for heading in headings:
        if heading.start() > selected.start() and len(heading.group(1)) <= 2:
            end = heading.start()
            break
    return text[selected.end():end]


def _source_target(line: str) -> str | None:
    stripped = line.strip()
    if stripped.startswith(("- ", "* ")):
        stripped = stripped[2:].lstrip()
    if not stripped.startswith(SOURCE_MARKER):
        return None
    return stripped[len(SOURCE_MARKER):].strip()


def _strict_pillar_items(section: str, pillar: str) -> tuple[tuple[str, str, str], ...]:
    items: list[tuple[str, str, str]] = []
    current_title: str | None = None
    source_urls: list[str] = []

    def finish() -> None:
        nonlocal current_title, source_urls
        if current_title is None:
            return
        if len(source_urls) != 1:
            raise RegistryInputError(
                "weekly Pillar item must have exactly one source link"
            )
        items.append((pillar, canonical_title(current_title), canonical_url(source_urls[0])))
        current_title, source_urls = None, []

    for line in section.splitlines():
        title = PILLAR_ITEM.fullmatch(line)
        if title:
            finish()
            current_title = title.group(1)
            continue
        target = _source_target(line)
        if target is None:
            continue
        if current_title is None:
            raise RegistryInputError("weekly Pillar has an orphan source link")
        _validate_public_http_url(target)
        source_urls.append(target)
        if len(source_urls) > 1:
            raise RegistryInputError(
                "weekly Pillar item must have exactly one source link"
            )
    finish()
    return tuple(items)


def parse_strict_weekly_report(path: Path) -> ParsedReport:
    """Parse a weekly report while requiring unambiguous item/source links."""

    report_path = Path(path)
    try:
        raw = report_path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RegistryInputError("weekly report must be readable UTF-8") from exc
    strict_items = tuple(
        item
        for pillar in ("A", "B")
        for item in _strict_pillar_items(_pillar_section(text, pillar), pillar)
    )
    try:
        report = parse_historical_report(report_path)
    except Exception as exc:
        raise RegistryInputError("weekly report structure is invalid") from exc
    if report.cadence != "weekly":
        raise RegistryInputError("report is not in the weekly Pillar format")
    if report.sha256 != hashlib.sha256(raw).hexdigest():
        raise RegistryInputError("weekly report changed during validation")
    parsed_items = tuple(
        (article.pillar, canonical_title(article.title), canonical_url(article.url))
        for article in report.articles
    )
    if parsed_items != strict_items:
        raise RegistryInputError("weekly Pillar item/source association is ambiguous")
    return report


def _sidecars(database: Path) -> tuple[Path, ...]:
    return tuple(
        Path(f"{database}{suffix}")
        for suffix in ("-wal", "-shm", "-journal")
        if Path(f"{database}{suffix}").exists()
    )


def _read_only_connection(database: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro&immutable=1", uri=True
        )
        connection.execute("PRAGMA query_only = ON")
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise RegistryBuildError("registry database could not be opened read-only") from exc


def load_registry_selection_snapshot(
    database: Path, source_dir: Path
) -> RegistrySelectionSnapshot:
    """Read a synchronized immutable supported Registry snapshot without mutation."""

    database = Path(database).resolve()
    source_dir = Path(source_dir).resolve()
    if not database.is_file():
        raise RegistryInputError("registry database does not exist")
    if not source_dir.is_dir():
        raise RegistryInputError("registry source directory does not exist")
    if database == source_dir or source_dir in database.parents:
        raise RegistryInputError("registry database must be external to source history")
    if _sidecars(database):
        raise RegistryInputError("registry database has active SQLite sidecars")

    try:
        reports = parse_report_directory(source_dir)
    except Exception as exc:
        raise RegistryBuildError("registry source report history is invalid") from exc
    if not reports:
        raise RegistryInputError("registry source report history is empty")

    connection = _read_only_connection(database)
    try:
        try:
            validate_registry_contract(connection)
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RegistryBuildError("registry database failed integrity validation")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RegistryBuildError("registry database failed foreign-key validation")
            stored_reports = tuple(
                connection.execute(
                    "SELECT report_date, filename, report_sha256 FROM reports ORDER BY report_date"
                )
            )
            expected_reports = tuple(
                (report.report_date, report.path.name, report.sha256) for report in reports
            )
            if stored_reports != expected_reports:
                raise RegistryInputError("registry and source history are not synchronized")
            urls = frozenset(
                row[0]
                for row in connection.execute("SELECT canonical_url FROM articles")
            )
            try:
                expected_urls = set()
                for report in reports:
                    for article in report.articles:
                        _validate_public_http_url(article.url)
                        expected_urls.add(canonical_url(article.url))
                expected_urls = frozenset(expected_urls)
            except (RegistryInputError, UnicodeError, ValueError) as exc:
                raise RegistryBuildError(
                    "registry source report history is invalid"
                ) from exc
            if urls != expected_urls:
                raise RegistryInputError(
                    "registry article graph and source history are not synchronized"
                )
        except SchemaContractError as exc:
            raise RegistryInputError("registry schema contract is invalid") from exc
        except (RegistryInputError, RegistryBuildError):
            raise
        except sqlite3.DatabaseError as exc:
            raise RegistryBuildError("registry database is unreadable or corrupt") from exc
    finally:
        connection.close()
    if _sidecars(database):
        raise RegistryBuildError("registry read unexpectedly created SQLite sidecars")
    return RegistrySelectionSnapshot(canonical_urls=urls)


def plan_selection(
    payload: dict[str, Any], *, historical_urls: Iterable[str]
) -> dict[str, Any]:
    """Return a deterministic safe-ID-only plan for already validated candidates."""

    report_date, candidates = _validate_payload(payload)
    ordered = tuple(candidate for pillar in ("A", "B") for candidate in candidates if candidate.pillar == pillar)
    historical = frozenset(historical_urls)
    url_owners: dict[str, SelectionCandidate] = {}
    title_owners: dict[str, SelectionCandidate] = {}
    decisions: list[dict[str, str]] = []

    for candidate in ordered:
        url_key = canonical_url(candidate.url)
        title_key = canonical_title(candidate.title)
        policy = classify_document(url_key)
        url_owner = url_owners.get(url_key)
        title_owner = title_owners.get(title_key)

        if not policy.publication_eligible:
            disposition, reason = "rejected", "publication_ineligible"
        elif url_owner is not None:
            reason = (
                "same_pillar_canonical_url"
                if url_owner.pillar == candidate.pillar
                else "cross_pillar_canonical_url"
            )
            disposition = "rejected"
        elif title_owner is not None:
            disposition, reason = "rejected", "same_run_canonical_title"
        elif url_key in historical:
            disposition, reason = "rejected", "historical_url_seen"
        else:
            disposition, reason = "selected", "new_article"

        url_owners.setdefault(url_key, candidate)
        title_owners.setdefault(title_key, candidate)
        decisions.append(
            {
                "candidate_id": candidate.candidate_id,
                "pillar": candidate.pillar,
                "disposition": disposition,
                "reason": reason,
            }
        )

    reason_counts: dict[str, int] = {}
    for decision in decisions:
        reason = decision["reason"]
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "report_date": report_date,
        "counts": {
            "total": len(decisions),
            "selected": sum(item["disposition"] == "selected" for item in decisions),
            "rejected": sum(item["disposition"] == "rejected" for item in decisions),
            "pillar_a": sum(item["pillar"] == "A" for item in decisions),
            "pillar_b": sum(item["pillar"] == "B" for item in decisions),
            "reasons": dict(sorted(reason_counts.items())),
        },
        "decisions": decisions,
    }


def plan_registry_selection(
    database: Path, source_dir: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    """Plan candidate selection against an exact synchronized Registry baseline."""

    _validate_payload(payload)
    snapshot = load_registry_selection_snapshot(database, source_dir)
    return plan_selection(payload, historical_urls=snapshot.canonical_urls)


def candidate_payload(
    report_date: str, candidates: Iterable[SelectionCandidate]
) -> dict[str, Any]:
    """Build the strict public contract for trusted in-process callers."""

    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "report_date": report_date,
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "pillar": candidate.pillar,
                "title": candidate.title,
                "summary": candidate.summary,
                "url": candidate.url,
            }
            for candidate in candidates
        ],
    }
