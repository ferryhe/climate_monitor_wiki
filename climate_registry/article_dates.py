"""Import source-backed article collection and information-date observations."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from climate_monitor.dedupe import canonical_url

from .contract import validate_registry_contract


OBSERVATIONS_SCHEMA = "article-date-observations.v1"
_RFC3339 = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)\Z")
_DATE = re.compile(r"\d{4}-\d\d-\d\d\Z")
_MONTH = re.compile(r"\d{4}-\d\d\Z")
_YEAR = re.compile(r"\d{4}\Z")
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


def _typed_date_kind(evidence: Any) -> str | None:
    if not isinstance(evidence, Mapping):
        return None
    kind = re.sub(r"[\s_]+", "-", str(evidence.get("date_kind") or "").strip().casefold())
    if kind in {"published", "publication", "date-published", "published-date"}:
        return "published"
    if kind in {"updated", "update", "modified", "last-modified", "date-modified", "updated-date"}:
        return "updated"
    if kind in {"revised", "revision", "version", "version-revision", "revision-date", "revised-date", "version-date"}:
        return "revision"
    if kind in {"event", "activity", "event-date", "activity-date"}:
        return "event"
    return None


def _typed_information_date(value: Any, precision: Any) -> str:
    if precision is None:
        if isinstance(value, str) and _DATE.fullmatch(value):
            return _information_date(value)
        raise ArticleDateImportError("typed page_information date must state its precision")
    if precision == "day":
        return _information_date(value)
    if precision == "month" and isinstance(value, str) and _MONTH.fullmatch(value):
        try:
            date.fromisoformat(value + "-01")
        except ValueError as exc:
            raise ArticleDateImportError("page_information month is invalid") from exc
        return value
    if precision == "year" and isinstance(value, str) and _YEAR.fullmatch(value):
        year = int(value)
        if 1 <= year <= 9999:
            return value
    raise ArticleDateImportError("typed page_information date must match its stated precision")



def report_observation_date(value: Any, evidence: Any) -> str | None:
    """Recognize a sourced report day without inventing a collection instant."""
    if (not isinstance(evidence, Mapping) or evidence.get("date_basis") != "daily_or_weekly_report_date"
            or evidence.get("report_date") != value or not isinstance(value, str)
            or _DATE.fullmatch(value) is None):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def select_article_dates(observations, *, collection_times=(), acquisition_observations=()):
    """Select precise source dates before the explicitly recorded report-day fallback."""
    collected, information, reports, updates, revisions, page_publications = [], [], [], [], [], []
    for item in observations:
        if item["kind"] == "collection":
            report_day = report_observation_date(item["observed_at"], item["evidence"])
            if report_day:
                reports.append(report_day)
            else:
                collected.append(_collection_timestamp(item["observed_at"]))
        elif item["kind"] == "page_information":
            evidence = item.get("evidence")
            precision = evidence.get("date_precision") if isinstance(evidence, Mapping) else None
            kind = _typed_date_kind(evidence)
            information_value = (
                _typed_information_date(item["observed_at"], precision)
                if kind else _information_date(item["observed_at"])
            )
            if not kind:
                information.append(information_value)
            if kind == "published":
                page_publications.append(information_value)
            elif kind == "updated":
                updates.append(information_value)
            elif kind == "revision":
                revisions.append(information_value)
        else:
            raise ArticleDateImportError("invalid article date observation kind")
    for value in collection_times:
        if value and value not in reports:
            collected.append(_collection_timestamp(value))
    collected_at = max(collected, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))) if collected else None
    information_date = max(information) if information else None
    qualified_publication = []
    for item in acquisition_observations:
        if (item.get("publication_date_evidence")
                and item.get("processing_status", "complete") == "complete"
                and item.get("selection_status", "selected") == "selected"):
            try:
                qualified_publication.append(_information_date(item.get("publication_date")))
            except ArticleDateImportError:
                pass  # Non-day source precision remains unconfirmed, as in range selection.
    qualified_publication.extend(page_publications)
    publication_date = max(qualified_publication) if qualified_publication else None
    updated_date = max(updates) if updates else None
    revision_date = max(revisions) if revisions else None
    report_date = max(reports) if reports else None
    basis = ("collection_time" if collected_at else "information_date" if information_date else
             "publication_date" if publication_date else "report_date" if report_date else "unknown")
    return {"collected_at": collected_at, "information_date": information_date,
            "publication_date": publication_date, "report_date": report_date, "date_basis": basis,
            "updated_date": updated_date, "revision_date": revision_date}


def _article_event_from_observation(item: Mapping[str, Any]) -> dict[str, Any] | None:
    evidence = item.get("evidence")
    if _typed_date_kind(evidence) != "event":
        return None
    if not isinstance(evidence, Mapping):
        raise ArticleDateImportError("event date evidence must be an object")
    precision = evidence.get("date_precision")
    if precision not in {"day", "month", "year"}:
        raise ArticleDateImportError("event date evidence must state day, month, or year precision")
    start = _typed_information_date(evidence.get("event_start_date", item.get("observed_at")), precision)
    observed = _typed_information_date(item.get("observed_at"), precision)
    end = _typed_information_date(evidence.get("event_end_date", start), precision)
    if observed != start:
        raise ArticleDateImportError("event observed_at must equal its start date")
    if start > end:
        raise ArticleDateImportError("event end date must not precede its start date")
    event_name = evidence.get("event_name")
    if not isinstance(event_name, str) or not event_name.strip():
        raise ArticleDateImportError("event evidence must include event_name")
    raw_date = evidence.get("date_text")
    if not isinstance(raw_date, str) or not raw_date.strip():
        raise ArticleDateImportError("event evidence must include its source date text")
    event_type = evidence.get("event_type")
    if event_type not in {"meeting", "conference", "summit", "webinar", "deadline", "retrospective"}:
        raise ArticleDateImportError("event evidence has an unsupported event_type")
    status = evidence.get("event_status", "scheduled")
    if status not in {"scheduled", "tentative", "postponed", "cancelled", "conflict", "retrospective"}:
        raise ArticleDateImportError("event evidence has an unsupported event_status")
    canonical = json.dumps([event_name.casefold().strip(), start, end, precision], ensure_ascii=False, separators=(",", ":"))
    event_id = "reviewed-article-event-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    source_url = evidence.get("source_url")
    urls = list(dict.fromkeys(value for value in (source_url, evidence.get("event_url")) if isinstance(value, str) and value.strip()))
    return {
        "event_id": event_id, "article_id": item.get("article_id"),
        "name": event_name.strip(), "event_type": event_type, "status": status,
        "date_precision": precision, "start_date": start, "end_date": end,
        "raw_date": raw_date.strip(), "raw_time_text": evidence.get("raw_time_text"),
        "event_timezone": evidence.get("event_timezone"), "organizer": evidence.get("event_organizer"),
        "location": evidence.get("event_location"), "online_url": evidence.get("event_url") or source_url,
        "relevance_reason": evidence.get("event_summary"), "source_urls": urls,
        "source_count": 1, "verification_status": "verified" if evidence.get("review_status") == "pass" else "unchecked",
        "verification_basis": "independent_article_date_review" if evidence.get("review_status") == "pass" else None,
        "origin": "reviewed_article_page", "collection_status": "collected", "access_status": "accessible",
    }


def select_article_events(observations: Any) -> list[dict[str, Any]]:
    """Project only typed, source-backed activity dates into the Dates view."""
    events = []
    for item in observations or []:
        if not isinstance(item, Mapping) or item.get("kind") != "page_information":
            continue
        event = _article_event_from_observation(item)
        if event is not None and event["verification_status"] == "verified":
            events.append(event)
    return events


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
        evidence = item["evidence"]
        if not isinstance(evidence, Mapping) or not set(_EVIDENCE_REQUIRED) <= set(evidence):
            raise ArticleDateImportError("evidence is missing source identity fields")
        evidence = dict(evidence)
        for field in _EVIDENCE_REQUIRED:
            if field == "record_id" and type(evidence[field]) is int:
                evidence[field] = str(evidence[field])
            _text(evidence[field], f"evidence.{field}")
        report_day = report_observation_date(item["observed_at"], evidence) if kind == "collection" else None
        if kind == "collection":
            item["observed_at"] = report_day or _collection_timestamp(item["observed_at"])
        elif _typed_date_kind(evidence):
            item["observed_at"] = _typed_information_date(item["observed_at"], evidence.get("date_precision"))
        else:
            item["observed_at"] = _information_date(item["observed_at"])
        if kind == "collection" and report_day is None:
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
        if _typed_date_kind(evidence) == "event":
            _article_event_from_observation(item)
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
    for row in connection.execute(
        """SELECT observed_at,evidence_json FROM article_date_observations
           WHERE article_id=? AND observation_kind='collection'""", (article_id,),
    ):
        if report_observation_date(row[0], json.loads(row[1])) is None:
            _collection_timestamp(row[0])
            return True
    return bool(connection.execute(
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
    entity_kind: str = "article",
    write: bool = False,
    recorded_at: str | None = None,
) -> dict[str, int]:
    """Validate and optionally append evidence to a migrated Registry."""
    from .persistent import _exclusive_database_lock
    from .publication import require_publication_migration, resolve_database, stage_entities
    if entity_kind not in {"article", "pdf_article"}:
        raise ArticleDateImportError("entity_kind must be article or pdf_article")
    table = "article_date_observations" if entity_kind == "article" else "pdf_article_date_observations"
    entity_table = "articles" if entity_kind == "article" else "pdf_intake_articles"
    path = resolve_database(_database_path(database))
    items = validate_observations(payload)
    mode = "rw" if write else "ro"
    with _exclusive_database_lock(path) if write else nullcontext():
        connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            version = validate_registry_contract(connection)
            minimum_version = 18 if entity_kind == "article" else 25
            if version < minimum_version:
                raise ArticleDateImportError(
                    f"Registry schema {minimum_version} or later is required for {entity_kind}; run the normal migration first"
                )
            if write:
                require_publication_migration(connection)
                connection.execute("BEGIN IMMEDIATE")
            if entity_kind == "pdf_article" and any(item["observation_kind"] != "page_information"
                    or _typed_date_kind(item["evidence"]) is None for item in items):
                raise ArticleDateImportError("PDF article date evidence must be typed page_information")
            collection_articles = {
                item["article_id"] for item in items if item["observation_kind"] == "collection"
                and report_observation_date(item["observed_at"], item["evidence"]) is None
            }
            for item in items:
                article = connection.execute(
                    f"SELECT canonical_url FROM {entity_table} WHERE article_id=?", (item["article_id"],)
                ).fetchone()
                if article is None or article["canonical_url"] != item["canonical_url"]:
                    raise ArticleDateImportError("article_id and exact canonical_url do not match Registry")
                if (item["observation_kind"] == "page_information"
                        and _typed_date_kind(item["evidence"]) is None
                        and entity_kind == "article"
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
                    f"""SELECT canonical_url, observed_at, evidence_json FROM {table}
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
                    f"""INSERT INTO {table}(
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
                if counts["inserted"]:
                    entity_ids = {item["article_id"] for item in items}
                    if entity_kind == "article":
                        stage_entities(connection, article_ids=entity_ids)
                    else:
                        from .publication import canonical_entity
                        linked_core_ids = {
                            owner_id
                            for identity in entity_ids
                            for owner_kind, owner_id in [canonical_entity(connection, "pdf_article", identity)]
                            if owner_kind == "article"
                        }
                        stage_entities(connection, article_ids=linked_core_ids, pdf_article_ids=entity_ids)
                    from .display import refresh_display
                    refresh_display(connection)
                connection.commit()
            return counts
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
