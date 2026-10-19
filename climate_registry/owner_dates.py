"""Selective article date checks inherit an exact approved factual scope."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .acquisition_review import digest, timestamp
from .information_checks import _json, _sha, DATE_ONLY_CHECK_VERSION

DATE_CHECK_VERSION = DATE_ONLY_CHECK_VERSION
DATE_REVIEW_VERSION = "owner-agent-publication-date-review.v1"
DATE_FIELDS_REVIEW_VERSION = "owner-agent-article-date-fields-review.v1"
ACTIVITY_REVIEW_VERSION = "owner-agent-article-activity-dates-review.v1"
OVERLAY = "publication_date_only"
DATE_FIELDS_SCOPE = "article_date_fields_only"
ACTIVITY_SCOPE = "article_activity_dates_only"
DATE_PUBLIC_FIELDS = {"publication_date", "updated_date", "revision_date"}


def _event_row(row):
    from .article_dates import _typed_date_kind
    if not isinstance(row, dict) or row.get("observation_kind", row.get("kind")) != "page_information":
        return False
    evidence = row.get("evidence")
    if evidence is None:
        evidence = json.loads(row.get("evidence_json") or "null")
    return _typed_date_kind(evidence) == "event"


def validate_activity_dates(values, entry):
    from .article_dates import select_article_events
    from .owner_checks import _dates, _nonempty
    if not isinstance(values, list) or not values:
        raise ValueError("activity dates require one or more typed event observations")
    capture, target = entry["capture"], entry["target"]
    if capture["requested_url"] != target["source_url"]:
        raise ValueError("activity date capture source URL differs from the exact article source")
    seen = set()
    allowed = {"date_kind", "date_precision", "event_name", "event_type", "event_status",
        "event_start_date", "event_end_date", "date_text", "review_status", "source_url", "target_url",
        "field", "text", "raw_time_text", "event_timezone", "event_organizer", "event_location", "event_url", "event_summary"}
    for value in values:
        if not isinstance(value, dict) or set(value) != {"observed_at", "evidence"}:
            raise ValueError("activity date must contain only observed_at and evidence")
        evidence = value["evidence"]
        if (not isinstance(evidence, dict) or set(evidence) - allowed
                or evidence.get("date_kind") != "event" or evidence.get("review_status") != "pass"
                or evidence.get("source_url") != capture["requested_url"]
                or evidence.get("target_url") != target["source_url"]
                or not _nonempty(evidence.get("field"))):
            raise ValueError("activity date needs passing review and exact source and target URLs")
        if evidence.get("event_status") in {"cancelled", "retrospective"} or evidence.get("event_type") == "retrospective":
            raise ValueError("activity date scope requires an event eligible for the Dates projection")
        for key in ("event_name", "date_text", "text", "raw_time_text", "event_timezone",
                "event_organizer", "event_location", "event_url", "event_summary"):
            if (key == "text" or key in evidence) and (not _nonempty(evidence.get(key)) or evidence[key] not in capture["text"]):
                raise ValueError("activity fact lacks literal source capture evidence: " + key)
        events = select_article_events([{"kind": "page_information", **value}])
        if len(events) != 1:
            raise ValueError("activity date must be a reviewed typed event")
        event = events[0]
        dates = _dates(evidence["date_text"])
        if (event["start_date"] != dates["start_date"] or event["end_date"] != (dates["end_date"] or dates["start_date"])
                or event["date_precision"] != dates["date_precision"]):
            raise ValueError("activity date bounds must match the literal source precision")
        if event["event_id"] in seen:
            raise ValueError("activity dates contain a duplicate event")
        seen.add(event["event_id"])


def _append_activity_rows(view, entry, manifest, proof):
    from climate_monitor.dedupe import canonical_url
    from .article_dates import select_article_events
    tables = view["tables"]
    url = canonical_url(entry["target"]["source_url"])
    appended = False
    for owner_table, date_table in (("articles", "article_date_observations"),
            ("pdf_intake_articles", "pdf_article_date_observations")):
        for owner in tables.get(owner_table, []):
            if canonical_url(owner["canonical_url"]) != url:
                continue
            for value in entry["public_fields"]["activity_dates"]:
                identity = "owner-activity-" + digest([proof["review_id"], owner["article_id"], value])
                evidence = {**value["evidence"], "source_system": "owner_agent",
                    "database": manifest["plan"]["registry_database"], "table": "registry_reviews",
                    "record_id": identity, "review_id": proof["review_id"], "match_basis": "exact_owner_activity_review",
                    "capture_sha256": entry["capture"]["sha256"], "final_url": entry["capture"]["final_url"],
                    "captured_at": entry["capture"]["captured_at"]}
                tables.setdefault(date_table, []).append({"observation_id": identity, "article_id": owner["article_id"],
                    "canonical_url": owner["canonical_url"], "observation_kind": "page_information",
                    "observed_at": value["observed_at"], "source_system": "owner_agent",
                    "source_database": manifest["plan"]["registry_database"], "source_table": "registry_reviews",
                    "source_record_id": identity, "evidence_json": _json(evidence), "recorded_at": manifest["checked_at"]})
                appended = True
    if not appended:
        raise ValueError("activity date source has no exact inherited article owner")
    events = {}
    for table in ("article_date_observations", "pdf_article_date_observations"):
        for row in tables.get(table, []):
            if not _event_row(row):
                continue
            for event in select_article_events([{"kind": "page_information", "observed_at": row["observed_at"],
                    "evidence": json.loads(row["evidence_json"])}]):
                events[event["event_id"]] = event
    view.setdefault("derived_display", {}).update(activity_dates=list(events.values()), is_activity=bool(events))


def _date_fields(plan):
    if plan.get("scope") == ACTIVITY_SCOPE:
        return ["activity_dates", "is_activity"]
    if plan.get("scope") == "article_publication_date_only":
        return ["publication_date"]
    fields = plan.get("date_fields")
    if (plan.get("scope") != DATE_FIELDS_SCOPE or not isinstance(fields, list)
            or not fields or len(fields) != len(set(fields)) or set(fields) - DATE_PUBLIC_FIELDS):
        raise ValueError("date-field scope must select one or more supported date fields")
    return fields


def _base(snapshot):
    base = dict(snapshot)
    overlay = base.pop(OVERLAY)
    if "inherited_date_overlay" in overlay:
        base[OVERLAY] = overlay["inherited_date_overlay"]
    if digest(base) != overlay["baseline_candidate_sha256"]:
        raise ValueError("date-only inherited candidate hash differs")
    return base, overlay


def _baseline_view(base, reviews):
    from .verification import evaluate_snapshot, permitted_view
    sha = digest(base)
    if not reviews or any(row.get("candidate_sha256") != sha or row.get("status") not in {"pass", "accepted_legacy"}
            for row in reviews):
        raise ValueError("date-only baseline approval is missing or belongs to another candidate")
    for row in reviews:
        receipt = json.loads(row["receipt_json"])
        if row["review_id"] not in {digest([sha, row["status"], receipt]), digest([sha, receipt])}:
            raise ValueError("date-only baseline approval receipt hash differs")
    verdict = evaluate_snapshot(base, reviews=reviews)
    view = permitted_view(base, verdict)
    if not verdict["eligible"] or view is None:
        raise ValueError("date-only baseline has no approved factual projection")
    return view, verdict


def _nondate(value, date_fields=("publication_date",)):
    """Ignore only date facts/provenance and the new version's root identity."""
    value = deepcopy(value)
    value.pop("_approved_candidate_sha256", None)
    display = value.get("derived_display", {})
    for field in date_fields:
        for key in (field, field + "_evidence", field + "_verification"):
            display.pop(key, None)
    if "activity_dates" in date_fields:
        for table in ("article_date_observations", "pdf_article_date_observations"):
            rows = value.get("tables", {}).get(table, [])
            value.get("tables", {})[table] = [row for row in rows if not _event_row(row)]
    return value


def date_plan(connection, plan, *, fields=None, activity_dates_only=False):
    if plan["entity_kind"] not in {"article", "pdf_article"}:
        raise ValueError("publication date scope is available only for articles")
    state = plan["expected_state"]
    sha = state["published_candidate_sha256"]
    if not sha or state["is_visible"] != 1:
        raise ValueError("publication date scope requires a visible approved article")
    row = connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()
    if row is None or digest(json.loads(row[0])) != sha:
        raise ValueError("date-only published candidate is missing or changed")
    base = json.loads(row[0])
    if (base["entity_kind"], base["entity_id"]) != (plan["entity_kind"], plan["entity_id"]):
        raise ValueError("date-only baseline belongs to another article")
    reviews = [dict(zip(("review_id", "candidate_sha256", "status", "reviewed_at", "receipt_json"), row))
        for row in connection.execute("SELECT * FROM registry_reviews WHERE candidate_sha256=? AND status IN ('pass','accepted_legacy') ORDER BY review_id", (sha,))]
    view, _ = _baseline_view(base, reviews)
    # Targets come from the approved snapshot, never a newer unapproved source.
    legacy = fields is None and not activity_dates_only
    selected_fields = ["activity_dates", "is_activity"] if activity_dates_only else ["publication_date"] if legacy else list(dict.fromkeys(fields))
    if not activity_dates_only and (not selected_fields or set(selected_fields) - DATE_PUBLIC_FIELDS):
        raise ValueError("date-field scope must select one or more supported date fields")
    scope = ACTIVITY_SCOPE if activity_dates_only else "article_publication_date_only" if legacy else DATE_FIELDS_SCOPE
    plan = {**plan, "targets": base["information_targets"], "scope": scope,
        "baseline_candidate_sha256": sha, "baseline_public_projection_sha256": digest(view),
        "nondate_scope_sha256": digest(_nondate(view, selected_fields)), "baseline_reviews": reviews}
    if not legacy and not activity_dates_only:
        plan["date_fields"] = selected_fields
    return {"schema_version": DATE_CHECK_VERSION, "plan": plan, "actor": {"id": "", "task_id": ""},
        "authorization_reference": "", "audit_reference": "", "checked_at": "", "observations": []}


def validate_date_manifest(manifest):
    from .owner_checks import PLAN_VERSION, _actor, _nonempty, validate_observation
    plan = manifest["plan"]
    if (plan.get("schema_version") != PLAN_VERSION or plan.get("scope") not in {"article_publication_date_only", DATE_FIELDS_SCOPE, ACTIVITY_SCOPE}
            or plan.get("entity_kind") not in {"article", "pdf_article"} or "taxonomy" in manifest):
        raise ValueError("unsupported article date owner scope")
    fields = _date_fields(plan)
    _actor(manifest["actor"])
    if not all(_nonempty(manifest.get(key)) for key in ("authorization_reference", "audit_reference")):
        raise ValueError("owner authorization and audit references are required")
    timestamp(manifest["checked_at"])
    entries = manifest["observations"]
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError("date check requires one exact same-article date source")
    entry = entries[0]
    if entry["target"] not in plan["targets"] or "editorial_metadata" in entry:
        raise ValueError("date source is outside the inherited approved scope")
    if set(entry.get("public_fields", {})) != (set(fields) - {"is_activity"}):
        raise ValueError("date evidence must exactly match the selected date fields")
    validate_observation(entry["target"], entry,
        publication_date_only=plan.get("scope") == "article_publication_date_only",
        date_fields_only=plan.get("scope") == DATE_FIELDS_SCOPE,
        activity_dates_only=plan.get("scope") == ACTIVITY_SCOPE)
    if timestamp(entry["capture"]["captured_at"]) > timestamp(manifest["checked_at"]):
        raise ValueError("source capture is later than its check")


def _frozen_scope(snapshot):
    base, overlay = _base(snapshot)
    manifest = overlay["manifest"]
    validate_date_manifest(manifest)
    plan = manifest["plan"]
    fields = _date_fields(plan)
    if (plan["baseline_candidate_sha256"] != overlay["baseline_candidate_sha256"]
            or plan["baseline_candidate_sha256"] != plan["expected_state"]["published_candidate_sha256"]
            or plan["expected_state"]["is_visible"] != 1
            or (plan["entity_kind"], plan["entity_id"]) != (base["entity_kind"], base["entity_id"])
            or plan["targets"] != base["information_targets"]):
        raise ValueError("date-only baseline identity or approved source binding differs")
    view, verdict = _baseline_view(base, plan["baseline_reviews"])
    if (digest(view) != plan["baseline_public_projection_sha256"]
            or digest(_nondate(view, fields)) != plan["nondate_scope_sha256"]):
        raise ValueError("date-only inherited public or non-date scope hash differs")
    run, attempt = overlay["run"], overlay["attempt"]
    target = manifest["observations"][0]["target"]
    packet = {"schema_version": DATE_CHECK_VERSION, "owner_manifest": manifest}
    encoded = _json(packet)
    expected_attempt = _attempt(manifest, run["run_id"])
    if (run != _run(manifest) or attempt != expected_attempt or run["input_json"] != encoded
            or hashlib.sha256(encoded.encode()).hexdigest() != run["input_sha256"]
            or target not in base["information_targets"]):
        raise ValueError("date-only frozen run or attempt differs")
    return view, verdict, overlay


def _run(manifest):
    encoded = _json({"schema_version": DATE_CHECK_VERSION, "owner_manifest": manifest})
    return {"run_id": "owner-date-" + digest(manifest), "input_json": encoded,
        "input_sha256": hashlib.sha256(encoded.encode()).hexdigest(), "created_at": manifest["checked_at"],
        "completed_at": manifest["checked_at"], "status": "complete", "item_count": 1, "completed_count": 1}


def _attempt(manifest, run_id):
    target = manifest["observations"][0]["target"]
    packet = {"schema_version": DATE_CHECK_VERSION, "owner_manifest": manifest}
    return {"attempt_id": "owner-date-attempt-" + digest([run_id, target]), "run_id": run_id,
        "occurrence_id": target["occurrence_id"] if target["entity_kind"] == "pdf_article" else None,
        "core_article_id": target["entity_id"] if target["entity_kind"] == "article" else None,
        "source_url": target["source_url"], "source_revision_sha256": target["source_revision_sha256"],
        "checked_at": manifest["checked_at"], "access_status": "accessible", "verification_status": "verified",
        "packet_json": _json(packet), "packet_sha256": _sha(packet)}


def ingest_date_check(connection, database, manifest, apply):
    from .owner_checks import _plan
    from .publication import stage_snapshot
    run = _run(manifest)
    plan = manifest["plan"]
    latest = connection.execute("SELECT c.snapshot_json FROM registry_publication p JOIN registry_candidates c ON c.candidate_sha256=p.latest_candidate_sha256 WHERE p.entity_kind=? AND p.entity_id=?", (plan["entity_kind"], plan["entity_id"])).fetchone()
    if latest and json.loads(latest[0]).get(OVERLAY, {}).get("manifest") == manifest:
        snapshot = json.loads(latest[0])
        _current_binding(connection, snapshot, allow_published=True)
        _frozen_scope(snapshot)
        return {"status": "no-op", "run_id": run["run_id"], "candidate_sha256": digest(snapshot)}
    current = date_plan(connection, _plan(connection, database, plan["entity_kind"], plan["entity_id"]),
        fields=plan.get("date_fields") if plan.get("scope") == DATE_FIELDS_SCOPE else None,
        activity_dates_only=plan.get("scope") == ACTIVITY_SCOPE)["plan"]
    if current != plan:
        raise ValueError("date-only owner check plan is stale")
    if not apply:
        return {"status": "dry_run", "run_id": run["run_id"]}
    base = json.loads(connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (plan["baseline_candidate_sha256"],)).fetchone()[0])
    attempt = _attempt(manifest, run["run_id"])
    for table, row in (("article_check_runs", run), ("article_check_attempts", attempt)):
        columns = list(row)
        connection.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(row.values()))
    overlay = {"baseline_candidate_sha256": plan["baseline_candidate_sha256"],
        "manifest": manifest, "run": run, "attempt": attempt}
    if OVERLAY in base:
        overlay["inherited_date_overlay"] = base[OVERLAY]
    snapshot = {**base, OVERLAY: overlay}
    _frozen_scope(snapshot)
    sha = stage_snapshot(connection, snapshot, basis=DATE_CHECK_VERSION)
    return {"status": "staged", "run_id": run["run_id"], "candidate_sha256": sha,
        "review_schema_version": _review_version(plan["scope"]),
        "fact_publication": "awaiting_independent_owner_review"}


def _bindings(snapshot):
    _, _, overlay = _frozen_scope(snapshot)
    manifest = overlay["manifest"]
    plan = manifest["plan"]
    return {"baseline_candidate_sha256": plan["baseline_candidate_sha256"],
        "baseline_public_projection_sha256": plan["baseline_public_projection_sha256"],
        "nondate_scope_sha256": plan["nondate_scope_sha256"],
        "date_evidence_sha256": digest(manifest["observations"]),
        "attempt_id": overlay["attempt"]["attempt_id"], "packet_sha256": overlay["attempt"]["packet_sha256"]}


def _review_version(scope):
    return ACTIVITY_REVIEW_VERSION if scope == ACTIVITY_SCOPE else DATE_REVIEW_VERSION if scope == "article_publication_date_only" else DATE_FIELDS_REVIEW_VERSION


def date_review_template(snapshot):
    plan = snapshot[OVERLAY]["manifest"]["plan"]
    review_scope = plan["scope"]
    return {"schema_version": _review_version(review_scope),
        "review_scope": review_scope,
        "candidate_sha256": digest(snapshot), "source_checks": _bindings(snapshot),
        "actor": {"id": "", "task_id": ""}, "authorization_reference": "", "audit_reference": "",
        "reviewed_at": "", "decision": "", "reason": ""}


def validate_date_review(snapshot, receipt):
    from .owner_checks import _actor, _nonempty
    plan = snapshot[OVERLAY]["manifest"]["plan"]
    review_scope = plan["scope"]
    review_version = _review_version(review_scope)
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != review_version or receipt.get("decision") != "pass"
            or receipt.get("review_scope") != review_scope
            or receipt.get("candidate_sha256") != digest(snapshot)
            or receipt.get("source_checks") != _bindings(snapshot)):
        raise ValueError("date-only independent review must pass the exact candidate and inherited scope")
    _actor(receipt["actor"])
    manifest = snapshot[OVERLAY]["manifest"]
    if any(receipt["actor"][key] == manifest["actor"][key] for key in ("id", "task_id")):
        raise ValueError("date-only review requires an independent actor and task")
    if timestamp(receipt["reviewed_at"]) < timestamp(manifest["checked_at"]):
        raise ValueError("date-only independent review predates source check")
    if not all(_nonempty(receipt.get(key)) for key in ("authorization_reference", "audit_reference", "reason")):
        raise ValueError("date-only independent review references and reason are required")


def date_verdict(snapshot, reviews):
    try:
        _, verdict, _ = _frozen_scope(snapshot)
        sha = digest(snapshot)
        for row in reviews:
            if row.get("candidate_sha256") != sha or row.get("status") != "pass":
                continue
            receipt = json.loads(row["receipt_json"])
            if row.get("review_id") != digest([sha, "pass", receipt]):
                continue
            validate_date_review(snapshot, receipt)
            return {**verdict, OVERLAY: {"receipt": receipt, "review_id": row["review_id"]}}
    except (ValueError, KeyError, TypeError):
        pass
    return {"eligible": False, "primary": None, "observations": {}, "reviewed_summary": None,
        "reasons": ["date_only_independent_review_or_inherited_scope_invalid"]}


def date_public_view(snapshot, verdict):
    view, _, overlay = _frozen_scope(snapshot)
    proof = verdict[OVERLAY]
    manifest = overlay["manifest"]
    entry = manifest["observations"][0]
    fields = _date_fields(manifest["plan"])
    display = view.setdefault("derived_display", {})
    if manifest["plan"]["scope"] == ACTIVITY_SCOPE:
        _append_activity_rows(view, entry, manifest, proof)
    for field in fields if manifest["plan"]["scope"] != ACTIVITY_SCOPE else []:
        date_proof = entry["field_evidence"][field]
        evidence = {"text": date_proof["excerpt"], "date_kind": date_proof["date_kind"],
            "source_url": entry["capture"]["requested_url"], "final_url": entry["capture"]["final_url"],
            "capture_sha256": entry["capture"]["sha256"], "source_identity": entry["source_identity"],
            "source_system": "owner_agent", "table": "registry_reviews", "record_id": proof["review_id"]}
        if date_proof.get("date_precision") is not None:
            evidence["source_date_precision"] = date_proof["date_precision"]
        if date_proof.get("date_format") is not None:
            evidence["date_format"] = date_proof["date_format"]
        if date_proof.get("date_normalization") == "month_midpoint":
            evidence["source_date_precision"] = "month"
            evidence["date_normalization"] = "month_midpoint"
        date_verification = {"verification_basis": DATE_CHECK_VERSION,
            "review_scope": manifest["plan"]["scope"],
            "baseline_candidate_sha256": overlay["baseline_candidate_sha256"], "review_id": proof["review_id"],
            "reviewer": proof["receipt"]["actor"], "actor": manifest["actor"], "checked_at": manifest["checked_at"],
            "source_url": entry["target"]["source_url"], "capture_sha256": entry["capture"]["sha256"]}
        display.update({field: entry["public_fields"][field], field + "_evidence": evidence,
            field + "_verification": date_verification})
    # Readers apply the date after validating the inherited source's own fields.
    view["_approved_candidate_sha256"] = digest(snapshot)
    if digest(_nondate(view, fields)) != manifest["plan"]["nondate_scope_sha256"]:
        raise ValueError("date-field publication would change approved non-date facts")
    return view


def _current_binding(connection, snapshot, *, allow_published=False):
    from .publication import snapshot_entity
    plan = snapshot[OVERLAY]["manifest"]["plan"]
    current = connection.execute("SELECT latest_candidate_sha256,published_candidate_sha256,is_visible FROM registry_publication WHERE entity_kind=? AND entity_id=?", (snapshot["entity_kind"], snapshot["entity_id"])).fetchone()
    allowed = {plan["baseline_candidate_sha256"]}
    if allow_published:
        allowed.add(digest(snapshot))
    if (current is None or current[0] != digest(snapshot) or current[1] not in allowed
            or current[2] != plan["expected_state"]["is_visible"]):
        raise ValueError("date-only latest, publication or visibility binding changed")
    live = snapshot_entity(connection, snapshot["entity_kind"], snapshot["entity_id"])
    if digest(live) != plan["source_snapshot_sha256"]:
        raise ValueError("date-only live source changed; stage and review again")


def approve_date_check(connection, database, snapshot, receipt, apply):
    from .publication import _approve
    validate_date_review(snapshot, receipt)
    sha = digest(snapshot)
    review_id = digest([sha, "pass", receipt])
    existing = connection.execute("SELECT 1 FROM registry_reviews WHERE review_id=?", (review_id,)).fetchone()
    _current_binding(connection, snapshot, allow_published=bool(existing))
    proof = {"review_id": review_id, "candidate_sha256": sha, "status": "pass", "receipt_json": _json(receipt)}
    verdict = date_verdict(snapshot, [proof])
    if not verdict["eligible"]:
        raise ValueError("date-only inherited scope or independent review is invalid")
    view = date_public_view(snapshot, verdict)
    baseline, _, _ = _frozen_scope(snapshot)
    _same_public_dtos(connection, database, baseline, view,
        date_fields=_date_fields(snapshot[OVERLAY]["manifest"]["plan"]))
    if existing:
        return {"status": "no-op", "review_id": review_id}
    if not apply:
        return {"status": "dry_run", "review_id": review_id}
    _approve(connection, sha, receipt)
    return {"status": "approved", "review_id": review_id, "candidate_sha256": sha}


def _same_public_dtos(connection, database, baseline, view, *, date_fields=("publication_date",)):
    """A reader-derived non-date change aborts the same publication transaction."""
    from .read_api import RegistryReader
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])

    fields = date_fields
    date_keys = {key for field in fields for key in (field, field + "_evidence", field + "_verification")}

    def nondate(value):
        if isinstance(value, dict):
            value = {key: nondate(item) for key, item in value.items() if key not in date_keys
                and key not in {"published_candidate_sha256", "candidate_sha256"}}
            if "activity_dates" in fields and not value.get("date_observations"):
                value.pop("date_observations", None)
            return value
        if isinstance(value, list):
            return [nondate(item) for item in value if not ("activity_dates" in fields and _event_row(item))]
        return value

    def dtos(snapshot):
        values = []
        if snapshot["entity_kind"] == "article":
            values.extend((reader._article_from_snapshot(snapshot), reader._article_card_from_snapshot(snapshot)))
        for row in snapshot["tables"].get("pdf_intake_articles", []):
            scoped = {**snapshot, "entity_id": row["article_id"]}
            values.append(reader._pdf_article_from_snapshot(scoped))
        return nondate(values)

    with reader.public_snapshot(direct=True, source_connection=connection):
        if digest(dtos(baseline)) != digest(dtos(view)):
            raise ValueError("date-field publication would change approved non-date public DTOs")
