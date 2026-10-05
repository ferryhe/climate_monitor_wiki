"""Import source-backed article collection and information-date observations."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from climate_monitor.dedupe import canonical_url

from .contract import validate_registry_contract


OBSERVATIONS_SCHEMA = "article-date-observations.v1"
_RFC3339 = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)\Z")
_DATE = re.compile(r"\d{4}-\d\d-\d\d\Z")
_EVIDENCE_REQUIRED = ("source_system", "database", "table", "record_id", "source_url", "match_basis")


class ArticleDateImportError(ValueError):
    """Invalid or conflicting article date evidence."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ArticleDateImportError(f"{field} must be a trimmed non-empty string")
    return value


def _collection_timestamp(value: Any) -> str:
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        raise ArticleDateImportError("collection observed_at must be RFC3339 with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArticleDateImportError("collection observed_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ArticleDateImportError("collection observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _information_date(value: Any) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise ArticleDateImportError("page_information observed_at must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ArticleDateImportError("page_information observed_at is invalid") from exc
    return parsed.isoformat()


def validate_observations(payload: Any) -> list[dict[str, Any]]:
    if (not isinstance(payload, Mapping) or set(payload) != {"schema_version", "observations"}
            or payload.get("schema_version") != OBSERVATIONS_SCHEMA
            or not isinstance(payload.get("observations"), list)):
        raise ArticleDateImportError(f"input must use {OBSERVATIONS_SCHEMA}")
    checked = []
    seen = {}
    duplicate_identities = set()
    for raw in payload["observations"]:
        if not isinstance(raw, Mapping) or set(raw) != {
            "article_id", "canonical_url", "observation_kind", "observed_at", "evidence"
        }:
            raise ArticleDateImportError("article date observation fields are invalid")
        item = dict(raw)
        item["article_id"] = _text(item["article_id"], "article_id")
        item["canonical_url"] = _text(item["canonical_url"], "canonical_url")
        try:
            canonical = canonical_url(item["canonical_url"])
        except (TypeError, ValueError) as exc:
            raise ArticleDateImportError("canonical_url is invalid") from exc
        if canonical != item["canonical_url"]:
            raise ArticleDateImportError("canonical_url must already be canonical")
        kind = item["observation_kind"]
        if kind not in {"collection", "page_information"}:
            raise ArticleDateImportError("observation_kind must be collection or page_information")
        item["observed_at"] = (
            _collection_timestamp(item["observed_at"])
            if kind == "collection" else _information_date(item["observed_at"])
        )
        evidence = item["evidence"]
        if not isinstance(evidence, Mapping) or not set(_EVIDENCE_REQUIRED) <= set(evidence):
            raise ArticleDateImportError("evidence is missing source identity fields")
        evidence = dict(evidence)
        for field in _EVIDENCE_REQUIRED:
            if field == "record_id" and type(evidence[field]) is int:
                evidence[field] = str(evidence[field])
            _text(evidence[field], f"evidence.{field}")
        if kind == "collection":
            source_record = (evidence["source_system"], evidence["table"])
            if source_record not in {
                ("web_listening", "site_snapshots"),
                ("web_listening", "changes"),
                ("web_listening", "observations"),
                ("climate_registry", "article_fetches"),
            }:
                raise ArticleDateImportError("collection evidence must identify a verified acquisition record")
            if source_record == ("climate_registry", "article_fetches") and (
                evidence.get("match_basis") != "successful_article_fetch"
                or type(evidence.get("http_status")) is not int
                or not 200 <= evidence["http_status"] <= 299
            ):
                raise ArticleDateImportError("article_fetches collection evidence must be successful HTTP 2xx")
        item["evidence"] = evidence
        identity = (
            item["article_id"], kind, evidence["source_system"], evidence["database"],
            evidence["table"], evidence["record_id"],
        )
        normalized = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        prior = seen.setdefault(identity, normalized)
        if prior != normalized:
            raise ArticleDateImportError("the same source record has conflicting date observations")
        if identity in duplicate_identities:
            continue
        duplicate_identities.add(identity)
        item["_identity"] = identity
        item["_evidence_json"] = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        checked.append(item)
    return checked


def _database_path(value: str | Path) -> Path:
    path = Path(value)
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
    except OSError as exc:
        raise ArticleDateImportError(f"database does not exist: {path}") from exc
    if not path.is_absolute() or path != resolved or not path.is_file():
        raise ArticleDateImportError("database must be an absolute canonical regular file")
    return path


def _has_collection(connection: sqlite3.Connection, article_id: str) -> bool:
    return bool(connection.execute(
        """SELECT 1 FROM article_date_observations
           WHERE article_id=? AND observation_kind='collection' LIMIT 1""",
        (article_id,),
    ).fetchone() or connection.execute(
        """SELECT 1 FROM article_fetches f
           JOIN article_content_versions c
             ON c.article_id=f.article_id AND c.content_version_id=f.content_version_id
           WHERE f.article_id=? AND f.fetch_status='success'
             AND f.http_status BETWEEN 200 AND 299 LIMIT 1""",
        (article_id,),
    ).fetchone())


def import_observations(
    database: str | Path,
    payload: Any,
    *,
    write: bool = False,
    recorded_at: str | None = None,
) -> dict[str, int]:
    """Validate and optionally append evidence to a migrated Registry."""
    path = _database_path(database)
    items = validate_observations(payload)
    mode = "rw" if write else "ro"
    connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        version = validate_registry_contract(connection)
        if version < 18:
            raise ArticleDateImportError("Registry schema 18 or later is required; run the normal migration first")
        if write:
            connection.execute("BEGIN IMMEDIATE")
        collection_articles = {
            item["article_id"] for item in items if item["observation_kind"] == "collection"
        }
        for item in items:
            article = connection.execute(
                "SELECT canonical_url FROM articles WHERE article_id=?", (item["article_id"],)
            ).fetchone()
            if article is None or article["canonical_url"] != item["canonical_url"]:
                raise ArticleDateImportError("article_id and exact canonical_url do not match Registry")
            if (item["observation_kind"] == "page_information"
                    and (item["article_id"] in collection_articles
                         or _has_collection(connection, item["article_id"]))):
                raise ArticleDateImportError("page_information is allowed only when no collection evidence exists")

        counts = {"inserted": 0, "already_present": 0}
        stamp = recorded_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        stamp = _collection_timestamp(stamp)
        for item in items:
            evidence = item["evidence"]
            identity = item["_identity"]
            source = (item["article_id"], item["observation_kind"], *identity[2:])
            old = connection.execute(
                """SELECT canonical_url, observed_at, evidence_json FROM article_date_observations
                   WHERE article_id=? AND observation_kind=? AND source_system=?
                     AND source_database=? AND source_table=? AND source_record_id=?""",
                source,
            ).fetchone()
            expected = (item["canonical_url"], item["observed_at"], item["_evidence_json"])
            if old is not None:
                if tuple(old) != expected:
                    raise ArticleDateImportError("stored source record conflicts with imported date evidence")
                counts["already_present"] += 1
                continue
            if not write:
                counts["inserted"] += 1
                continue
            stable_input = "\n".join(map(str, (item["article_id"], item["observation_kind"], *identity[2:])))
            observation_id = "article-date-" + hashlib.sha256(stable_input.encode()).hexdigest()[:32]
            connection.execute(
                """INSERT INTO article_date_observations(
                       observation_id, article_id, canonical_url, observation_kind, observed_at,
                       source_system, source_database, source_table, source_record_id,
                       evidence_json, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (observation_id, item["article_id"], item["canonical_url"], item["observation_kind"],
                 item["observed_at"], evidence["source_system"], evidence["database"],
                 evidence["table"], evidence["record_id"], item["_evidence_json"], stamp),
            )
            counts["inserted"] += 1
        if write:
            connection.commit()
        return counts
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
