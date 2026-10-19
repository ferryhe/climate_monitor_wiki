"""Owner-authorized source captures and a separate exact-candidate agent review.

These are owner-agent records, never native T1 executions. Original observations
stay immutable; only the independently approved, excerpt-supported scope is public.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from climate_delivery.io import transaction_lock
from climate_monitor.meeting_fields import MEETING_FIELDS
from .acquisition_review import digest, timestamp
from .errors import RegistryInputError
from .information_checks import KINDS, _json, _sha
from .persistent import _backup_connection, _backup_name, _exclusive_database_lock
from .publication import canonical_entity, require_publication_migration, resolve_database, snapshot_entity, stage_snapshot

CHECK_VERSION = "owner-agent-information-check.v1"
EDITORIAL_CHECK_VERSION = "owner-agent-information-check.v2"
PLAN_VERSION = "owner-agent-information-plan.v1"
REVIEW_VERSION = "owner-agent-information-review.v1"
EDITORIAL_REVIEW_VERSION = "owner-agent-information-review.v2"
EDITORIAL_BASIS = "source_capture_editorial_classification"


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _actor(value):
    if not isinstance(value, dict) or not all(_nonempty(value.get(k)) for k in ("id", "task_id")):
        raise ValueError("actual actor id and task_id are required")


def _url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source capture requires an HTTP(S) URL")


def _plan(connection, database, kind, identity):
    kind, identity = canonical_entity(connection, kind, identity)
    snapshot = snapshot_entity(connection, kind, identity)
    if not snapshot["information_targets"]:
        raise ValueError("entity has no current source observations")
    row = connection.execute("SELECT latest_candidate_sha256,published_candidate_sha256,is_visible FROM registry_publication WHERE entity_kind=? AND entity_id=?", (kind, identity)).fetchone()
    if row is None:
        raise ValueError("entity must be staged before owner verification")
    return {"schema_version": PLAN_VERSION, "registry_database": str(database), "entity_kind": kind, "entity_id": identity,
        "expected_state": dict(zip(("latest_candidate_sha256", "published_candidate_sha256", "is_visible"), row)),
        "source_snapshot_sha256": digest(snapshot), "targets": snapshot["information_targets"]}


def plan_owner_check(database, kind, identity, *, with_editorial_metadata=False, publication_date_only=False, date_fields=None, activity_dates_only=False):
    """Read-only live binding; observations are templates, never a factual PASS."""
    database = resolve_database(database)
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        require_publication_migration(connection)
        connection.execute("BEGIN")
        plan = _plan(connection, database, kind, identity)
        if sum(bool(value) for value in (publication_date_only, date_fields, activity_dates_only)) > 1:
            raise ValueError("article date scopes cannot be combined")
        if activity_dates_only:
            if with_editorial_metadata:
                raise ValueError("activity date scope cannot include editorial metadata")
            from .owner_dates import date_plan
            return date_plan(connection, plan, activity_dates_only=True)
        if publication_date_only:
            if with_editorial_metadata:
                raise ValueError("publication date scope cannot include editorial metadata")
            from .owner_dates import date_plan
            return date_plan(connection, plan)
        if date_fields:
            if with_editorial_metadata:
                raise ValueError("selected date fields cannot include editorial metadata")
            from .owner_dates import date_plan
            return date_plan(connection, plan, fields=date_fields)
    if with_editorial_metadata:
        if kind not in {"article", "pdf_article"}:
            raise ValueError("editorial metadata is available only for article sources")
        from climate_monitor.taxonomy import load_article_taxonomy
        selected = load_article_taxonomy()
        return {"schema_version": EDITORIAL_CHECK_VERSION, "taxonomy": {
            "schema_version": selected.schema_version, "taxonomy_id": selected.taxonomy_id,
            "sha256": selected.sha256, "definition_yaml": selected.definition_yaml},
            "plan": plan, "actor": {"id": "", "task_id": ""},
            "authorization_reference": "", "audit_reference": "", "checked_at": "", "observations": []}
    return {"schema_version": CHECK_VERSION, "plan": plan, "actor": {"id": "", "task_id": ""},
        "authorization_reference": "", "audit_reference": "", "checked_at": "", "observations": []}


def _editorial_metadata_sha256(value, taxonomy):
    return digest({"taxonomy": taxonomy, "editorial_metadata": value})


def _validate_editorial_metadata(value, *, taxonomy_identity, public_fields, capture):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"basis", "capture_sha256", "public_fields_sha256", "categories", "keywords", "evidence"}:
        raise ValueError("editorial metadata must have the exact v2 scope")
    from climate_monitor.taxonomy import frozen_article_taxonomy, validate_semantic_bundle
    taxonomy = frozen_article_taxonomy(taxonomy_identity)
    if (value["basis"] != EDITORIAL_BASIS or value["capture_sha256"] != capture["sha256"]
            or value["public_fields_sha256"] != digest(public_fields)):
        raise ValueError("editorial metadata source binding differs")
    evidence = value["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {"categories", "keywords"}:
        raise ValueError("editorial metadata evidence must cover categories and keywords")
    text = capture["text"]
    for key, values in (("categories", value["categories"]), ("keywords", value["keywords"])):
        proofs = evidence[key]
        if not isinstance(values, list) or not isinstance(proofs, list) or len(values) != len(proofs):
            raise ValueError("each editorial value needs one source-bound evidence item")
        for expected, proof in zip(values, proofs):
            if (not isinstance(proof, dict) or set(proof) != {"value", "excerpt", "record_context", "reason"}
                    or proof["value"] != expected or not _nonempty(proof.get("excerpt"))
                    or proof["excerpt"] not in text or not _nonempty(proof.get("record_context"))
                    or proof["record_context"] not in text or not _nonempty(proof.get("reason"))):
                raise ValueError("editorial evidence must be literal and bound to the same capture")
    validate_semantic_bundle({"schema_version": "article-semantic-bundle.v1", "taxonomy_id": taxonomy.taxonomy_id,
        "taxonomy_sha256": taxonomy.sha256, "summary": public_fields.get("summary", ""),
        "categories": value["categories"], "keywords": value["keywords"]}, taxonomy=taxonomy)
    return value


def _dates(text):
    from climate_monitor.pdf_intake import _parse_calendar_date
    value = re.sub(r"\s+to\s+", "-", text.strip(), flags=re.I)
    iso_month = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if iso_month:
        from datetime import date
        year, month = map(int, iso_month.groups())
        date(year, month, 1)
        return {"start_date": value, "end_date": None, "date_precision": "month"}
    if re.fullmatch(r"\d{1,2}-[A-Za-z]+-\d{4}", value):
        value = value.replace("-", " ")
    interval = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})\s*[-–—]\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", value)
    if interval:
        first_month, first_day, last_month, last_day, year = interval.groups()
        dates = _parse_calendar_date(f"{first_day} {first_month}-{last_day} {last_month} {year}")
        if not dates["end_date"] or dates["start_date"] > dates["end_date"]:
            raise ValueError("invalid or reversed owner date interval")
        return dates
    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?,?\s+(\d{4})", value)
    if match:
        month, start, end, year = match.groups()
        value = start + ("-" + end if end else "") + " " + month + " " + year
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        from datetime import date
        date.fromisoformat(value)
        return {"start_date": value, "end_date": None, "date_precision": "day"}
    return _parse_calendar_date(value)


def validate_observation(target, entry, *, publication_date_only=False, date_fields_only=False, activity_dates_only=False):
    """Validate actual archived UTF-8 tool output and literal public field scope.

    Semantic record association is the checker's and independent reviewer's job.
    Literal excerpts prevent a new assertion from masquerading as captured text.
    """
    if entry.get("target") != target:
        raise ValueError("owner target differs from the exact frozen source binding")
    capture = entry["capture"]
    if not isinstance(capture, dict):
        raise ValueError("source capture must be an object")
    for key in ("requested_url", "final_url", "title", "captured_at", "tool", "text"):
        if not _nonempty(capture.get(key)):
            raise ValueError("source capture is incomplete: " + key)
    date_only = publication_date_only or date_fields_only or activity_dates_only
    if capture["requested_url"] != target["source_url"] and not date_only:
        raise ValueError("capture requested URL differs from observation")
    _url(capture["requested_url"]); _url(capture["final_url"])
    timestamp(capture["captured_at"])
    raw = base64.b64decode(capture["bytes_base64"], validate=True)
    text = raw.decode("utf-8")
    if text != capture["text"] or hashlib.sha256(raw).hexdigest() != capture["sha256"]:
        raise ValueError("captured bytes, text or SHA differ")
    if date_only:
        identity = entry.get("source_identity")
        if (not isinstance(identity, dict) or set(identity) != {"article_url", "excerpt", "reason"}
                or identity["article_url"] != target["source_url"]
                or not _nonempty(identity["excerpt"]) or identity["excerpt"] not in text
                or not _nonempty(identity["reason"])):
            raise ValueError("date-only capture needs literal same-article source identity for independent review")
    fields = entry["public_fields"]
    evidence = entry["field_evidence"]
    comparisons = entry["original_comparisons"]
    if not isinstance(comparisons, dict) or any(not isinstance(value, dict) for value in comparisons.values()):
        raise ValueError("original field dispositions must be objects")
    requested_fields = set(entry.get("public_fields", {})) if isinstance(entry.get("public_fields"), dict) else set()
    required = {key for key, value in target["fields"].items() if value is not None and value != "" and key != "source_document_sha256"}
    if publication_date_only:
        required &= {"publication_date"}
    elif date_fields_only or activity_dates_only:
        required &= requested_fields
    if set(comparisons) != required or any(value.get("status") not in {"supported", "missing", "conflict"} for value in comparisons.values()):
        raise ValueError("every original field needs an honest disposition")
    original = "conflict" if any(v["status"] == "conflict" for v in comparisons.values()) else "partial" if any(v["status"] == "missing" for v in comparisons.values()) else "verified"
    kind = target["check_kind"]
    allowed = ({"title", "summary", "publication_date"} if kind == "articles" else set(MEETING_FIELDS) - {"source_urls", "relevance_reason"})
    if publication_date_only:
        allowed = {"publication_date"}
        if kind != "articles" or not isinstance(fields, dict) or set(fields) != allowed:
            raise ValueError("date-only scope requires exactly an article publication_date")
        proof = evidence.get("publication_date") if isinstance(evidence, dict) else None
        if not isinstance(proof, dict) or proof.get("date_kind") not in {"updated", "published"}:
            raise ValueError("date-only evidence must identify updated or published date")
    if date_fields_only:
        allowed = {"publication_date", "updated_date", "revision_date"}
        if kind != "articles" or not isinstance(fields, dict) or not fields or set(fields) - allowed:
            raise ValueError("date-field scope requires one or more supported article date fields")
        expected_kinds = {"publication_date": {"published", "updated"},
            "updated_date": {"updated"}, "revision_date": {"revision"}}
        if any(not isinstance(evidence.get(key), dict)
                or evidence[key].get("date_kind") not in expected_kinds[key] for key in fields):
            raise ValueError("date-field evidence must match each selected field")
    if activity_dates_only:
        allowed = {"activity_dates"}
        if kind != "articles" or not isinstance(fields, dict) or set(fields) != allowed:
            raise ValueError("activity date scope requires exactly activity_dates")
    if not isinstance(fields, dict) or set(fields) - allowed or set(evidence) != set(fields):
        raise ValueError("unsupported public field scope")
    access = entry.get("access_status")
    if access not in {"accessible", "unavailable", "failed"} or fields and access != "accessible":
        raise ValueError("unsupported source access cannot establish public facts")
    for key, value in fields.items():
        proof = evidence[key]
        if not isinstance(proof, dict):
            raise ValueError("field evidence must be an object")
        excerpt = proof.get("excerpt")
        if not _nonempty(excerpt) or excerpt not in text or proof.get("value") != value or not _nonempty(proof.get("record_context")) or proof["record_context"] not in text:
            raise ValueError("public field lacks literal record-specific capture evidence: " + key)
        if key == "activity_dates" and activity_dates_only:
            from .owner_dates import validate_activity_dates
            validate_activity_dates(value, entry)
        elif key in {"start_date", "end_date", "date_precision", "deadline_date", "publication_date", "updated_date", "revision_date"}:
            date_text = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", excerpt, flags=re.I) if date_only else excerpt
            if date_only:
                date_text = re.sub(r"(?<=\d)\s+of\s+", " ", date_text, flags=re.I).replace(",", "")
                date_text = re.sub(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.(?=\s)",
                    lambda match: "Sep" if match[1].casefold() == "sept" else match[1], date_text, flags=re.I)
                date_format = proof.get("date_format")
                if date_format is not None and date_format not in {"MM/DD/YYYY", "DD/MM/YYYY"}:
                    raise ValueError("unsupported explicit date-only date_format")
                if "/" in date_text:
                    from datetime import datetime
                    if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", date_text) and date_format is None:
                        pattern = "%Y/%m/%d"
                    elif re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", date_text) and date_format:
                        pattern = "%m/%d/%Y" if date_format == "MM/DD/YYYY" else "%d/%m/%Y"
                    else:
                        raise ValueError("slash publication date requires an explicit matching date_format")
                    date_text = datetime.strptime(date_text, pattern).date().isoformat()
                elif date_format is not None:
                    raise ValueError("explicit date_format differs from literal publication date")
            dates = _dates(date_text)
            normalization = proof.get("date_normalization") if date_only else None
            if normalization == "month_midpoint":
                if (dates["date_precision"] != "month" or dates["end_date"] is not None
                        or proof.get("source_date_precision") != "month"):
                    raise ValueError("month midpoint requires a month-precision source")
                expected = dates["start_date"] + "-15"
            elif normalization is not None or publication_date_only and proof.get("source_date_precision") is not None:
                raise ValueError("unsupported publication date normalization")
            else:
                expected = dates["start_date"] if key in {"deadline_date", "publication_date", "updated_date", "revision_date"} else dates[key]
            invalid_precision = dates["date_precision"] == "unknown" or (
                key in {"publication_date", "deadline_date", "updated_date", "revision_date"}
                and dates["date_precision"] != ("month" if normalization == "month_midpoint" else "day"))
            if value != expected or invalid_precision:
                raise ValueError("public date exceeds explicit source precision: " + key)
        elif key == "summary":
            if not _nonempty(value) or proof.get("support") != "complete":
                raise ValueError("public summary needs complete semantic support for independent review")
        elif not _nonempty(value) or value.casefold() not in excerpt.casefold():
            raise ValueError("public text must be an exact source excerpt: " + key)
    if fields:
        if kind == "articles" and not date_only:
            if not fields.get("title") or not fields.get("summary") or evidence["summary"].get("section") != "article_body":
                raise ValueError("article scope needs a useful source-body summary and title")
        elif kind != "articles" and (not fields.get("name") or not fields.get("date_precision") or not (fields.get("start_date") or fields.get("deadline_date"))):
            raise ValueError("meeting scope needs explicit identity and dates")
        if kind == "meetings":
            from climate_monitor.meetings import EVENT_TYPES, EVENT_STATUSES, DEADLINE_TYPES
            for key, allowed_values in (("event_type", EVENT_TYPES), ("status", EVENT_STATUSES), ("deadline_type", DEADLINE_TYPES)):
                if fields.get(key) is not None and fields[key] not in allowed_values:
                    raise ValueError("unsupported normalized meeting value: " + key)
            if target["entity_kind"] == "meeting" and not all(fields.get(key) for key in ("event_type", "status")):
                raise ValueError("native meeting scope needs supported event_type and status")
            if fields.get("start_date"):
                dates = _dates(evidence["start_date"]["excerpt"])
                if fields.get("end_date") != dates["end_date"] or fields.get("date_precision") != dates["date_precision"]:
                    raise ValueError("public meeting interval must retain the explicit source bounds")
    excluded = sorted(key for key, value in target["fields"].items() if value is not None and value != "" and key != "source_document_sha256" and fields.get(key) != value)
    if date_only:
        excluded = []
    if entry.get("excluded_fields") != excluded:
        raise ValueError("excluded original fields must be complete and exact")
    return {"public_fields": fields, "original_verification_status": original, "excluded_fields": excluded,
        "verification_status": "verified" if fields else "conflict" if original == "conflict" else "unchecked"}


def validate_manifest(manifest, *, require_current_taxonomy=True):
    from .owner_dates import DATE_CHECK_VERSION, validate_date_manifest
    if isinstance(manifest, dict) and manifest.get("schema_version") == DATE_CHECK_VERSION:
        return validate_date_manifest(manifest)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("plan"), dict):
        raise ValueError("owner check and plan must be objects")
    version = manifest.get("schema_version")
    if version not in {CHECK_VERSION, EDITORIAL_CHECK_VERSION} or manifest["plan"].get("schema_version") != PLAN_VERSION:
        raise ValueError("unsupported owner-agent check format")
    taxonomy = manifest.get("taxonomy") if version == EDITORIAL_CHECK_VERSION else None
    if version == EDITORIAL_CHECK_VERSION:
        if manifest["plan"].get("entity_kind") not in {"article", "pdf_article"} or not isinstance(taxonomy, dict):
            raise ValueError("editorial metadata checks require an article and frozen taxonomy")
        from climate_monitor.taxonomy import frozen_article_taxonomy, load_article_taxonomy
        frozen = frozen_article_taxonomy(taxonomy)
        if require_current_taxonomy:
            current = load_article_taxonomy()
            if (frozen.taxonomy_id, frozen.sha256) != (current.taxonomy_id, current.sha256):
                raise ValueError("new editorial metadata must use the current article taxonomy")
    _actor(manifest["actor"])
    for key in ("authorization_reference", "audit_reference"):
        if not _nonempty(manifest.get(key)):
            raise ValueError("owner authorization and audit references are required")
    timestamp(manifest["checked_at"])
    entries = manifest["observations"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("owner check observations must be nonempty")
    targets = manifest["plan"]["targets"]
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("target"), dict):
            raise ValueError("owner observation and target must be objects")
        target = entry["target"]
        key = (target["occurrence_id"], target["source_url"])
        if target not in targets or key in seen:
            raise ValueError("owner check contains a duplicate or unplanned target")
        seen.add(key)
        validate_observation(target, entry)
        if version == EDITORIAL_CHECK_VERSION:
            _validate_editorial_metadata(entry.get("editorial_metadata"), taxonomy_identity=taxonomy,
                public_fields=entry["public_fields"], capture=entry["capture"])
            if "editorial_metadata" not in entry:
                raise ValueError("v2 observation must explicitly include editorial_metadata or null")
        if timestamp(entry["capture"]["captured_at"]) > timestamp(manifest["checked_at"]):
            raise ValueError("source capture is later than its check")


@contextmanager
def _transaction(database, queue_dir, apply):
    """Use the same intake-writer ownership as projection activation."""
    from contextlib import nullcontext
    with (transaction_lock(Path(queue_dir), "intake-writer") if apply else nullcontext()):
        with (_exclusive_database_lock(database) if apply else nullcontext()):
            with closing(sqlite3.connect(database if apply else f"{database.as_uri()}?mode=ro", uri=not apply)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                require_publication_migration(connection)
                connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
                with connection:
                    yield connection
                    if apply:
                        from .display import refresh_display
                        refresh_display(connection)


def _backup(connection, database, backup_dir):
    root = Path(backup_dir); root.mkdir(parents=True, exist_ok=True)
    # A separate read handle can back up while this writer holds BEGIN IMMEDIATE.
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as source:
        target = root / _backup_name(database)
        _backup_connection(source, target)
    return str(target)


def _ingest_one(connection, database, manifest, apply):
    from .owner_dates import DATE_CHECK_VERSION, ingest_date_check
    if manifest["schema_version"] == DATE_CHECK_VERSION:
        return ingest_date_check(connection, database, manifest, apply)
    run_id = "owner-check-" + digest(manifest)
    plan = manifest["plan"]
    kind = manifest["observations"][0]["target"]["check_kind"]
    prefix = KINDS[kind]
    schema_version = manifest["schema_version"]
    review_version = EDITORIAL_REVIEW_VERSION if schema_version == EDITORIAL_CHECK_VERSION else REVIEW_VERSION
    repeat = connection.execute(f"SELECT input_json FROM {prefix}_check_runs WHERE run_id=?", (run_id,)).fetchone()
    if repeat:
        if json.loads(repeat[0]).get("owner_manifest") != manifest:
            raise ValueError("owner check repeat differs")
        latest = connection.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", (plan["entity_kind"], plan["entity_id"])).fetchone()
        row = connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (latest[0],)).fetchone()
        current = snapshot_entity(connection, plan["entity_kind"], plan["entity_id"])
        previous = json.loads(row[0]); previous.pop("derived_display", None)
        if digest(current) != digest(previous) or not any(value["run_id"] == run_id for value in previous["dependencies"].get(prefix + "_check_runs", [])):
            raise ValueError("owner check repeat has changed live sources or candidate")
        return {"status": "no-op", "run_id": run_id, "candidate_sha256": latest[0]}
    current = _plan(connection, database, plan["entity_kind"], plan["entity_id"])
    if current != plan:
        raise ValueError("owner check plan is stale; export and reconcile a new live plan")
    if not apply:
        return {"status": "dry_run", "run_id": run_id, "observation_count": len(manifest["observations"])}
    targets = [{k: v for k, v in entry["target"].items() if k != "check_kind"} for entry in manifest["observations"]]
    frozen = {"schema_version": schema_version, "registry_database": str(database), "kind": kind,
        "targets": targets, "owner_manifest": manifest}
    encoded = _json(frozen)
    outcomes = [validate_observation(entry["target"], entry) for entry in manifest["observations"]]
    status = "complete" if all(value["verification_status"] == "verified" for value in outcomes) else "partial"
    connection.execute(f"INSERT INTO {prefix}_check_runs(run_id,input_json,input_sha256,created_at,completed_at,status,item_count,completed_count) VALUES(?,?,?,?,?,?,?,?)",
        (run_id, encoded, hashlib.sha256(encoded.encode()).hexdigest(), manifest["checked_at"], manifest["checked_at"], status, len(targets), len(targets)))
    for target, entry, outcome in zip(targets, manifest["observations"], outcomes):
        packet = {"schema_version": schema_version, **target, **outcome, "checked_at": manifest["checked_at"],
            "access_status": entry["access_status"], "actor": manifest["actor"], "authorization_reference": manifest["authorization_reference"],
            "audit_reference": manifest["audit_reference"], "baseline": plan, "owner_observation": entry}
        if schema_version == EDITORIAL_CHECK_VERSION:
            packet["taxonomy"] = manifest["taxonomy"]
            packet["taxonomy_id"] = manifest["taxonomy"]["taxonomy_id"]
            packet["taxonomy_sha256"] = manifest["taxonomy"]["sha256"]
            packet["editorial_metadata"] = entry["editorial_metadata"]
            packet["editorial_metadata_sha256"] = _editorial_metadata_sha256(entry["editorial_metadata"], manifest["taxonomy"])
        packet["reader"] = {"content_hash": entry["capture"]["sha256"], "final_url": entry["capture"]["final_url"]}
        packet["website_candidate"] = outcome["public_fields"] if prefix == "meeting" and outcome["verification_status"] == "verified" else None
        native = not target["entity_kind"].startswith("pdf_")
        column = "core_article_id" if prefix == "article" else "event_source_id"
        columns = "attempt_id,run_id,occurrence_id,source_url,source_revision_sha256,checked_at,access_status,verification_status,packet_json,packet_sha256"
        values = ["owner-attempt-" + digest([run_id, target]), run_id, None if native else target["occurrence_id"], target["source_url"], target["source_revision_sha256"], manifest["checked_at"], entry["access_status"], outcome["verification_status"], _json(packet), _sha(packet)]
        if native:
            columns += "," + column
            values.append(target["entity_id"] if prefix == "article" else target["occurrence_id"])
        connection.execute(f"INSERT INTO {prefix}_check_attempts({columns}) VALUES({','.join('?' for _ in values)})", values)
    sha = stage_snapshot(connection, snapshot_entity(connection, plan["entity_kind"], plan["entity_id"]))
    return {"status": "staged", "run_id": run_id, "candidate_sha256": sha,
        "review_schema_version": review_version, "fact_publication": "awaiting_independent_owner_review"}

def ingest_owner_check(database, manifest, *, queue_dir, backup_dir=None, apply=False):
    """One object or a bounded list: preflight all, one backup, atomic writes."""
    try:
        database = resolve_database(database)
        manifests = manifest if isinstance(manifest, list) else [manifest]
        if not manifests or len(manifests) > 100:
            raise ValueError("owner check batch must contain 1 to 100 manifests")
        keys = set()
        for item in manifests:
            validate_manifest(item)
            if Path(item["plan"]["registry_database"]).resolve() != database:
                raise ValueError("owner plan belongs to another Registry")
            if len({entry["target"]["check_kind"] for entry in item["observations"]}) != 1:
                raise ValueError("one owner check run must use one information kind")
            key = (item["plan"]["entity_kind"], item["plan"]["entity_id"])
            if key in keys:
                raise ValueError("owner batch canonical entities must be unique")
            keys.add(key)
        if apply and backup_dir is None:
            raise ValueError("apply requires a backup directory")
        with _transaction(database, queue_dir, apply) as connection:
            results = [_ingest_one(connection, database, item, False) for item in manifests]
            backup = None
            if apply and any(item["status"] != "no-op" for item in results):
                backup = _backup(connection, database, backup_dir)
                results = [_ingest_one(connection, database, item, True) for item in manifests]
            if isinstance(manifest, list):
                return {"status": "staged" if apply else "dry_run", "results": results, "backup_path": backup}
            return {**results[0], "backup_path": backup}
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise RegistryInputError(str(exc)) from exc




def owner_packet(snapshot, target, row, packet):
    """Validate a frozen owner packet without consulting mutable live observations."""
    expected = {key: value for key, value in target.items() if key not in {"check_kind", "_row", "_native_binding"}}
    if any(packet.get(key) != value for key, value in expected.items()):
        raise ValueError("owner packet source binding differs")
    if any(packet.get(key) != row[key] for key in ("source_revision_sha256", "checked_at", "access_status", "verification_status")):
        raise ValueError("owner attempt columns differ from its packet")
    prefix = KINDS[target["check_kind"]]
    run = next((run for run in snapshot.get("dependencies", {}).get(prefix + "_check_runs", []) if run["run_id"] == row["run_id"]), None)
    if not run or hashlib.sha256(run["input_json"].encode()).hexdigest() != run["input_sha256"] or run["status"] not in {"complete", "partial"} or not run.get("completed_at"):
        raise ValueError("owner check run is incomplete")
    frozen = json.loads(run["input_json"])
    manifest = frozen["owner_manifest"]
    validate_manifest(manifest, require_current_taxonomy=False)
    frozen_targets = [{key: value for key, value in entry["target"].items() if key != "check_kind"} for entry in manifest["observations"]]
    if frozen.get("targets") != frozen_targets or run["created_at"] != manifest["checked_at"]:
        raise ValueError("owner frozen run targets differ")
    version = manifest["schema_version"]
    if (frozen.get("schema_version") != version or packet.get("schema_version") != version
            or frozen.get("registry_database") != manifest["plan"]["registry_database"]
            or row["run_id"] != "owner-check-" + digest(manifest) or frozen.get("kind") != target["check_kind"]
            or run["item_count"] != len(manifest["observations"]) or run["completed_count"] != run["item_count"]):
        raise ValueError("owner run identity differs")
    entry = packet["owner_observation"]
    bound = dict(expected, check_kind=target["check_kind"])
    if entry not in manifest["observations"] or entry["target"] != bound or packet["baseline"] != manifest["plan"] or snapshot.get("information_targets") != manifest["plan"]["targets"]:
        raise ValueError("owner frozen plan differs")
    outcome = validate_observation(bound, entry)
    if version == EDITORIAL_CHECK_VERSION:
        metadata = _validate_editorial_metadata(entry["editorial_metadata"], taxonomy_identity=manifest["taxonomy"],
            public_fields=entry["public_fields"], capture=entry["capture"])
        metadata_sha = _editorial_metadata_sha256(metadata, manifest["taxonomy"])
        if packet.get("taxonomy") != manifest["taxonomy"] or packet.get("editorial_metadata") != metadata or packet.get("editorial_metadata_sha256") != metadata_sha:
            raise ValueError("owner editorial metadata packet differs from its frozen scope")
        outcome = {**outcome, "editorial_metadata": metadata, "editorial_metadata_sha256": metadata_sha,
            "taxonomy_id": manifest["taxonomy"]["taxonomy_id"], "taxonomy_sha256": manifest["taxonomy"]["sha256"]}
    if any(packet.get(key) != value for key, value in outcome.items()) or any(packet.get(key) != manifest[key] for key in ("actor", "authorization_reference", "audit_reference", "checked_at")) or packet["access_status"] != entry["access_status"]:
        raise ValueError("owner packet conclusion differs")
    return outcome


def public_owner_verification(snapshot, target, row, packet, reviews):
    outcome = owner_packet(snapshot, target, row, packet)
    candidate = snapshot.get("_approved_candidate_sha256") or digest(snapshot)
    capture = packet["owner_observation"]["capture"]
    version = packet["schema_version"]
    review_version = EDITORIAL_REVIEW_VERSION if version == EDITORIAL_CHECK_VERSION else REVIEW_VERSION
    verification = {**outcome, "verification_basis": version, "actor": packet["actor"],
        "checked_at": row["checked_at"], "source_url": target["source_url"], "final_url": capture["final_url"],
        "capture_sha256": capture["sha256"]}
    if version == EDITORIAL_CHECK_VERSION:
        metadata = packet["editorial_metadata"]
        public_metadata = None if metadata is None else {"categories": list(metadata["categories"]), "keywords": list(metadata["keywords"])}
        verification.update(editorial_metadata=public_metadata,
            editorial_metadata_sha256=packet["editorial_metadata_sha256"], taxonomy_id=packet["taxonomy_id"],
            taxonomy_sha256=packet["taxonomy_sha256"],
            public_fields_sha256=packet["editorial_metadata"]["public_fields_sha256"] if metadata else digest(outcome["public_fields"]))
    for review in reviews:
        receipt = json.loads(review["receipt_json"])
        accepted_versions = {review_version}
        if version == CHECK_VERSION:
            accepted_versions.add(EDITORIAL_REVIEW_VERSION)
        if (receipt.get("schema_version") in accepted_versions and review.get("status") == "pass" and review.get("candidate_sha256") == candidate
                and review.get("review_id") == digest([candidate, "pass", receipt])):
            validate_review(snapshot, receipt)
            if outcome["verification_status"] != "verified":
                return "owner_public_scope_unsupported", None
            verified = {**verification, "reviewer": receipt["actor"], "review_id": review["review_id"]}
            if outcome["public_fields"].get("publication_date"):
                verified["publication_date_evidence"] = {"text": packet["owner_observation"]["field_evidence"]["publication_date"]["excerpt"],
                    "source_url": target["source_url"], "date_kind": "publication_date"}
            return None, verified
    return "owner_independent_review_missing", verification


def review_bindings(snapshot):
    from .verification import frozen_targets
    from .information_checks import attempt_identity, verified_packet_from_attempt
    values = []
    scopes = {}
    for target in frozen_targets(snapshot):
        prefix = KINDS[target["check_kind"]]
        rows = [row for row in snapshot["tables"].get(prefix + "_check_attempts", []) if attempt_identity(row, prefix) == target["occurrence_id"] and row["source_url"] == target["source_url"]]
        row = max(rows, key=lambda row: (row["checked_at"], row["attempt_id"]), default=None)
        if row is None:
            continue
        packet = verified_packet_from_attempt(row)
        if packet.get("schema_version") not in {CHECK_VERSION, EDITORIAL_CHECK_VERSION}:
            continue
        outcome = owner_packet(snapshot, target, row, packet)
        editorial_metadata = packet.get("editorial_metadata") if packet.get("schema_version") == EDITORIAL_CHECK_VERSION else None
        if outcome["public_fields"] or editorial_metadata is not None:
            scope = {"public_fields": outcome["public_fields"], "editorial_metadata": editorial_metadata}
            previous = scopes.setdefault(target["occurrence_id"], scope)
            if previous != scope:
                raise ValueError("owner sources disagree on the public or editorial scope of one observation")
        binding = {"attempt_id": row["attempt_id"], "packet_sha256": row["packet_sha256"], "capture_sha256": packet["owner_observation"]["capture"]["sha256"],
            "public_fields_sha256": digest(packet["public_fields"]), "actor": packet["actor"]}
        if packet["schema_version"] == EDITORIAL_CHECK_VERSION:
            binding.update(check_schema_version=EDITORIAL_CHECK_VERSION,
                editorial_metadata_sha256=packet["editorial_metadata_sha256"])
        values.append(binding)
    return sorted(values, key=digest)


def owner_information_ready(snapshot):
    """Scoped owner completion does not invent terminal checks for other targets."""
    try:
        if not any(any(version in row.get("packet_json", "") for version in (CHECK_VERSION, EDITORIAL_CHECK_VERSION)) for prefix in ("article", "meeting")
                for row in snapshot.get("tables", {}).get(prefix + "_check_attempts", [])):
            return False
        return bool(review_bindings(snapshot))
    except (ValueError, KeyError, TypeError):
        return False


def public_check_state(kind, verification):
    """Public derived state contains no capture body, original claims or run input."""
    fields = verification["public_fields"]
    candidate = dict(fields) if kind == "meetings" else None
    if candidate and candidate.get("event_timezone"):
        candidate["timezone"] = candidate["event_timezone"]
    return {"verification_status": "verified", "access_status": "accessible", "collection_status": "collected",
        "checked_at": verification["checked_at"], "collected_candidate": candidate, "canonical_event_id": None,
        "checks": [{"source_url": verification["source_url"], "final_url": verification["final_url"],
            "verification_status": "verified", "access_status": "accessible", "checked_at": verification["checked_at"],
            "website_candidate": candidate, "verification_basis": verification.get("verification_basis", CHECK_VERSION)}]}


def owner_review_template(database, candidate_sha256):
    database = resolve_database(database)
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        row = connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (candidate_sha256,)).fetchone()
        if row is None or digest(json.loads(row[0])) != candidate_sha256:
            raise RegistryInputError("owner review candidate not found or hash differs")
        from .owner_dates import OVERLAY, date_review_template
        if OVERLAY in json.loads(row[0]):
            return date_review_template(json.loads(row[0]))
        bindings = review_bindings(json.loads(row[0]))
    version = EDITORIAL_REVIEW_VERSION if any(binding.get("check_schema_version") == EDITORIAL_CHECK_VERSION for binding in bindings) else REVIEW_VERSION
    result = {"schema_version": version, "candidate_sha256": candidate_sha256, "source_checks": bindings,
        "actor": {"id": "", "task_id": ""}, "authorization_reference": "", "audit_reference": "",
        "reviewed_at": "", "decision": "", "reason": ""}
    if version == EDITORIAL_REVIEW_VERSION:
        result["review_scope"] = "source_fields_and_article_editorial_metadata"
    return result


def validate_review(snapshot, receipt):
    from .owner_dates import OVERLAY, validate_date_review
    if OVERLAY in snapshot:
        return validate_date_review(snapshot, receipt)
    if not isinstance(receipt, dict):
        raise ValueError("owner independent review must be an object")
    version = receipt.get("schema_version")
    if version not in {REVIEW_VERSION, EDITORIAL_REVIEW_VERSION} or receipt.get("candidate_sha256") != (snapshot.get("_approved_candidate_sha256") or digest(snapshot)) or receipt.get("decision") != "pass":
        raise ValueError("owner review must pass the exact new candidate")
    bindings = review_bindings(snapshot)
    has_editorial = any(binding.get("check_schema_version") == EDITORIAL_CHECK_VERSION for binding in bindings)
    if (version == EDITORIAL_REVIEW_VERSION) != has_editorial:
        raise ValueError("review schema does not match selected owner evidence scope")
    if version == EDITORIAL_REVIEW_VERSION and receipt.get("review_scope") != "source_fields_and_article_editorial_metadata":
        raise ValueError("v2 review must explicitly cover editorial metadata")
    _actor(receipt["actor"])
    timestamp(receipt["reviewed_at"])
    if not all(_nonempty(receipt.get(key)) for key in ("authorization_reference", "audit_reference", "reason")):
        raise ValueError("owner independent review references and reason are required")
    if not bindings or receipt.get("source_checks") != bindings or any(receipt["actor"][key] == binding["actor"][key] for binding in bindings for key in ("id", "task_id")):
        raise ValueError("independent reviewer and exact evidence bindings are required")
    if any(timestamp(receipt["reviewed_at"]) < timestamp(row["checked_at"]) for prefix in ("article", "meeting")
            for row in snapshot["tables"].get(prefix + "_check_attempts", []) if row["attempt_id"] in {item["attempt_id"] for item in bindings}):
        raise ValueError("independent review predates its source check")


def _approve_one(connection, database, receipt, apply):
    from .publication import _approve
    from .verification import evaluate_snapshot
    sha = receipt["candidate_sha256"]
    row = connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()
    if row is None:
        raise ValueError("owner review candidate does not exist")
    snapshot = json.loads(row[0])
    if digest(snapshot) != sha:
        raise ValueError("candidate hash differs")
    from .owner_dates import OVERLAY, approve_date_check
    if OVERLAY in snapshot:
        return approve_date_check(connection, database, snapshot, receipt, apply)
    validate_review(snapshot, receipt)
    plan = _plan(connection, database, snapshot["entity_kind"], snapshot["entity_id"])
    if plan["expected_state"]["latest_candidate_sha256"] != sha:
        raise ValueError("owner review is stale")
    repeat = connection.execute("SELECT 1 FROM registry_reviews WHERE review_id=?", (digest([sha, "pass", receipt]),)).fetchone()
    for prefix in ("article", "meeting"):
        for row in snapshot["tables"].get(prefix + "_check_attempts", []):
            packet = json.loads(row["packet_json"])
            if packet.get("schema_version") in {CHECK_VERSION, EDITORIAL_CHECK_VERSION} and row["attempt_id"] in {item["attempt_id"] for item in receipt["source_checks"]}:
                baseline = packet["baseline"]["expected_state"]
                if plan["expected_state"]["is_visible"] != baseline["is_visible"] or (not repeat and plan["expected_state"]["published_candidate_sha256"] != baseline["published_candidate_sha256"]):
                    raise ValueError("owner review publication or visibility binding changed")
    # A new live write cannot be hidden by a still-current latest pointer.
    current = snapshot_entity(connection, snapshot["entity_kind"], snapshot["entity_id"])
    candidate = dict(snapshot); candidate.pop("derived_display", None)
    if digest(current) != digest(candidate):
        raise ValueError("owner review live source changed; stage and review again")
    review_id = digest([sha, "pass", receipt])
    proof = {"review_id": review_id, "candidate_sha256": sha, "status": "pass", "receipt_json": _json(receipt)}
    if not evaluate_snapshot(snapshot, reviews=[proof])["eligible"]:
        raise ValueError("owner review has no eligible supported public scope")
    existing = connection.execute("SELECT 1 FROM registry_reviews WHERE review_id=?", (review_id,)).fetchone()
    if existing:
        return {"status": "no-op", "review_id": review_id}
    if not apply:
        return {"status": "dry_run", "review_id": review_id}
    _approve(connection, sha, receipt)
    return {"status": "approved", "review_id": review_id, "candidate_sha256": sha}


def approve_owner_check(database, receipt, *, queue_dir, backup_dir=None, apply=False):
    try:
        database = resolve_database(database)
        receipts = receipt if isinstance(receipt, list) else [receipt]
        if not receipts or len(receipts) > 100 or len({item["candidate_sha256"] for item in receipts}) != len(receipts):
            raise ValueError("owner review batch must contain 1 to 100 unique candidates")
        if apply and backup_dir is None:
            raise ValueError("apply requires a backup directory")
        with _transaction(database, queue_dir, apply) as connection:
            results = [_approve_one(connection, database, item, False) for item in receipts]
            backup = None
            if apply and any(item["status"] != "no-op" for item in results):
                backup = _backup(connection, database, backup_dir)
                results = [_approve_one(connection, database, item, True) for item in receipts]
            if isinstance(receipt, list):
                return {"status": "approved" if apply else "dry_run", "results": results, "backup_path": backup}
            return {**results[0], "backup_path": backup}
    except (ValueError, KeyError, TypeError) as exc:
        raise RegistryInputError(str(exc)) from exc
