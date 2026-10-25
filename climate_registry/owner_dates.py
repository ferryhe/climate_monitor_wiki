"""Selective article date checks inherit an exact approved factual scope."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

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
DATE_SEMANTICS = "typed-article-dates.v2"
MANUAL_AUTHORIZATION = "human-article-date-authorization.v1"


def _event_row(row):
    from .article_dates import _typed_date_kind
    if not isinstance(row, dict) or row.get("observation_kind", row.get("kind")) != "page_information":
        return False
    evidence = row.get("evidence")
    if evidence is None:
        evidence = json.loads(row.get("evidence_json") or "null")
    return _typed_date_kind(evidence) == "event"


def validate_activity_dates(values, entry, *, manual=False):
    from .article_dates import select_article_events, _article_event_from_observation
    from .owner_checks import _dates, _nonempty
    if not isinstance(values, list) or not values:
        raise ValueError("activity dates require one or more typed event observations")
    capture, target = entry.get("capture", {}), entry["target"]
    if not manual and capture["requested_url"] != target["source_url"]:
        raise ValueError("activity date capture source URL differs from the exact article source")
    seen = set()
    allowed = {"date_kind", "date_precision", "event_name", "event_type", "event_status",
        "event_start_date", "event_end_date", "date_text", "review_status", "source_url", "target_url",
        "field", "text", "raw_time_text", "event_timezone", "event_organizer", "event_location", "event_url", "event_summary"}
    if manual:
        allowed = {"date_kind", "date_precision", "event_name", "event_type", "event_status",
            "event_start_date", "event_end_date", "date_text", "event_id"}
    for value in values:
        if not isinstance(value, dict) or set(value) != {"observed_at", "evidence"}:
            raise ValueError("activity date must contain only observed_at and evidence")
        evidence = value["evidence"]
        if not isinstance(evidence, dict) or set(evidence) - allowed or evidence.get("date_kind") != "event":
            raise ValueError("activity date identity or allowed date fields are invalid")
        if manual and not (allowed - {"event_id"}) <= set(evidence):
            raise ValueError("manual activity requires explicit identity, bounds and precision")
        if not manual and (evidence.get("review_status") != "pass"
                or evidence.get("source_url") != capture["requested_url"]
                or evidence.get("target_url") != target["source_url"]
                or not _nonempty(evidence.get("field"))):
            raise ValueError("activity date needs passing review and exact source and target URLs")
        if evidence.get("event_status") in {"cancelled", "retrospective"} or evidence.get("event_type") == "retrospective":
            raise ValueError("activity date scope requires an event eligible for the Dates projection")
        for key in ("event_name", "date_text", "text", "raw_time_text", "event_timezone",
                "event_organizer", "event_location", "event_url", "event_summary"):
            if (key == "text" and not manual or key in evidence) and (not _nonempty(evidence.get(key))
                    or not manual and evidence[key] not in capture["text"]):
                raise ValueError("activity fact lacks literal source capture evidence: " + key)
        observation = {"kind": "page_information", **value}
        events = [_article_event_from_observation(observation)] if manual else select_article_events([observation])
        if len(events) != 1:
            raise ValueError("activity date must be a reviewed typed event")
        event = events[0]
        date_text = evidence["date_text"]
        if event["event_id"] in seen:
            raise ValueError("activity dates contain a duplicate event")
        seen.add(event["event_id"])
        if manual:
            expected = event["start_date"] if event["start_date"] == event["end_date"] else event["start_date"] + " to " + event["end_date"]
            if date_text != expected:
                raise ValueError("manual activity text must match its explicit bounds and precision")
            continue
        if not re.search(r"\b\d{4}\b", date_text):
            years = set(re.findall(r"\b\d{4}\b", event["name"]))
            if len(years) != 1:
                raise ValueError("activity date without a year requires one unambiguous year in the literal event name")
            date_text = date_text + " " + next(iter(years))
        dates = _dates(date_text)
        if (dates["end_date"] is None and re.search(r"[-–—]|\bto\b", date_text, flags=re.I)
                and not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?|\d{1,2}-[A-Za-z]+-\d{4}", date_text)):
            raise ValueError("activity date interval must have a valid explicit end date")
        if (event["start_date"] != dates["start_date"] or event["end_date"] != (dates["end_date"] or dates["start_date"])
                or event["date_precision"] != dates["date_precision"]):
            raise ValueError("activity date bounds must match the literal source precision")


def _activity_events(view):
    from .article_dates import select_article_events
    observations = [{"kind": "page_information", "observed_at": row["observed_at"],
        "evidence": json.loads(row["evidence_json"])}
        for table in ("article_date_observations", "pdf_article_date_observations")
        for row in view.get("tables", {}).get(table, []) if _event_row(row)]
    return list({event["event_id"]: event for event in select_article_events(observations)}.values())


def _append_activity_rows(view, entry, manifest, proof):
    from climate_monitor.dedupe import canonical_url
    from .article_dates import _article_event_from_observation
    tables = view["tables"]
    manual = manifest.get("manual_authorization")
    display = view.setdefault("derived_display", {})
    prior = display.get("manual_date_overrides", {}).get("activity_dates")
    if prior and not manual:
        raise ValueError("active human activity date override requires an explicit human replacement")
    old_events = _activity_events(view)
    if manual:
        selected = {event.get("replaces_event_id") or event["event_id"] for event in old_events
            if any(value["evidence"].get("event_id") == event["event_id"]
                for value in entry["public_fields"]["activity_dates"] or [])}
        for table in ("article_date_observations", "pdf_article_date_observations"):
            retained = []
            for row in tables.get(table, []):
                evidence = json.loads(row.get("evidence_json") or "{}")
                if evidence.get("authority") == "human_override" and (manual.get("action") == "revoke" or evidence.get("event_id") in selected):
                    continue
                retained.append(row)
            tables[table] = retained
        remaining = [event for event in _activity_events(view) if event.get("authority") == "human_override"]
        if remaining and prior and (prior["scope"], prior.get("content_version_id")) != (manual["scope"], manual.get("content_version_id")):
            raise ValueError("activity date scope cannot mix record and version instructions")
        if manual.get("action") == "revoke":
            if not prior:
                raise ValueError("manual activity revocation requires an active override")
            display["manual_date_overrides"].pop("activity_dates")
            if not display["manual_date_overrides"]:
                display.pop("manual_date_overrides")
            for key in ("activity_dates_evidence", "activity_dates_verification"):
                display.pop(key, None)
            events = _activity_events(view)
            display.update(activity_dates=events, is_activity=bool(events))
            return
    source_rows = {table: [row for row in tables.get(table, [])
        if json.loads(row.get("evidence_json") or "{}").get("authority") != "human_override"]
        for table in ("article_date_observations", "pdf_article_date_observations")}
    source_events = _activity_events({"tables": source_rows})
    url = canonical_url(entry["target"]["source_url"])
    appended = False
    for owner_table, date_table in (("articles", "article_date_observations"),
            ("pdf_intake_articles", "pdf_article_date_observations")):
        for owner in tables.get(owner_table, []):
            if canonical_url(owner["canonical_url"]) != url:
                continue
            for value in entry["public_fields"]["activity_dates"]:
                identity = "owner-activity-" + digest([proof["review_id"], owner["article_id"], value])
                evidence = {**value["evidence"], "source_system": "human_instruction" if manual else "owner_agent",
                    "database": manifest["plan"]["registry_database"], "table": "registry_reviews",
                    "record_id": identity, "match_basis": "exact_human_activity_instruction" if manual else "exact_owner_activity_review"}
                if manual:
                    old_event = next((event for event in old_events
                        if event["event_id"] == value["evidence"].get("event_id")), {})
                    evidence["event_id"] = (old_event.get("replaces_event_id") or old_event.get("event_id")
                        or _article_event_from_observation({"kind": "page_information", **value})["event_id"])
                    for key, field in (("event_organizer", "organizer"), ("event_location", "location"),
                            ("event_timezone", "event_timezone"), ("raw_time_text", "raw_time_text"),
                            ("event_url", "online_url"), ("event_summary", "relevance_reason")):
                        if old_event.get(field):
                            evidence[key] = old_event[field]
                    evidence.update(authority="human_override", authorization_id=digest([manifest, "activity_dates"]),
                        instruction_reference=manifest["authorization_reference"],
                        source_url=entry["target"]["source_url"], target_url=entry["target"]["source_url"],
                        field="activity_dates", text=value["evidence"]["date_text"])
                else:
                    evidence.update(review_id=proof["review_id"], capture_sha256=entry["capture"]["sha256"],
                        final_url=entry["capture"]["final_url"], captured_at=entry["capture"]["captured_at"])
                tables.setdefault(date_table, []).append({"observation_id": identity, "article_id": owner["article_id"],
                    "canonical_url": owner["canonical_url"], "observation_kind": "page_information",
                    "observed_at": value["observed_at"], "source_system": "human_instruction" if manual else "owner_agent",
                    "source_database": manifest["plan"]["registry_database"], "source_table": "registry_reviews",
                    "source_record_id": identity, "evidence_json": _json(evidence), "recorded_at": manifest["checked_at"]})
                appended = True
    if not appended:
        raise ValueError("activity date source has no exact inherited article owner")
    events = _activity_events(view)
    display.update(activity_dates=events, is_activity=bool(events))
    if manual:
        evidence = {"authority": "human_override", "instruction_reference": manifest["authorization_reference"]}
        decision = {"schema_version": MANUAL_AUTHORIZATION, "actor": manifest["actor"],
            "authorized_by": manual["authorized_by"], "instruction": manual["instruction"],
            "instruction_reference": manifest["authorization_reference"], "audit_reference": manifest["audit_reference"],
            "executed_at": manifest["checked_at"], "field": "activity_dates", "old_value": old_events, "new_value": events,
            "date_fields": entry["field_evidence"]["activity_dates"]["fields"], "scope": manual["scope"],
            "content_version_id": manual.get("content_version_id"), "baseline_candidate_sha256": manifest["plan"]["baseline_candidate_sha256"],
            "authorization_id": digest([manifest, "activity_dates"]), "evidence": evidence,
            "source_effective": {"activity_dates": source_events,
                "activity_observations": source_rows},
            "activity_observations": {table: [row for row in tables.get(table, [])
                if json.loads(row.get("evidence_json") or "{}").get("authority") == "human_override"]
                for table in ("article_date_observations", "pdf_article_date_observations")}}
        display.setdefault("manual_date_overrides", {})["activity_dates"] = decision
        display.update(activity_dates_evidence=evidence, activity_dates_verification={"authority": "human_override",
            "verification_basis": MANUAL_AUTHORIZATION, "review_scope": ACTIVITY_SCOPE,
            "authorization_id": decision["authorization_id"]})


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
    overrides = display.get("manual_date_overrides", {})
    for field in date_fields:
        overrides.pop(field, None)
        for key in (field, field + "_evidence", field + "_verification"):
            display.pop(key, None)
    if not overrides:
        display.pop("manual_date_overrides", None)
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
        "date_semantics": DATE_SEMANTICS,
        "baseline_candidate_sha256": sha, "baseline_public_projection_sha256": digest(view),
        "nondate_scope_sha256": digest(_nondate(view, selected_fields)), "baseline_reviews": reviews}
    if not legacy:
        plan["baseline_content_version_id"] = next((row.get("current_content_version_id")
            for row in view.get("tables", {}).get("articles", []) if row.get("article_id") == view["entity_id"]), None)
    if not legacy and not activity_dates_only:
        plan["date_fields"] = selected_fields
        plan["source_date_resolution"] = "current-approved-version.v1"
    if activity_dates_only:
        plan["activity_targets"] = _activity_events(view)
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
    if manifest.get("manual_authorization") is not None:
        _validate_manual(manifest)
        return
    validate_observation(entry["target"], entry,
        publication_date_only=plan.get("scope") == "article_publication_date_only",
        date_fields_only=plan.get("scope") == DATE_FIELDS_SCOPE,
        activity_dates_only=plan.get("scope") == ACTIVITY_SCOPE,
        strict_date_types=plan.get("date_semantics") == DATE_SEMANTICS)
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
    if "baseline_content_version_id" in plan and plan["baseline_content_version_id"] != next(
            (row.get("current_content_version_id") for row in view.get("tables", {}).get("articles", [])
                if row.get("article_id") == view["entity_id"]), None):
        raise ValueError("date correction content version differs from its exact approved baseline")
    authorization = manifest.get("manual_authorization") or {}
    if authorization.get("scope") == "content_version":
        current = next((row.get("current_content_version_id") for row in view.get("tables", {}).get("articles", [])
            if row.get("article_id") == base["entity_id"]), None)
        if authorization.get("content_version_id") != current:
            raise ValueError("manual override content-version scope differs from the exact current body")
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
        "checked_at": manifest["checked_at"], "access_status": "unavailable" if manifest.get("manual_authorization") else "accessible",
        "verification_status": "unchecked" if manifest.get("manual_authorization") else "verified",
        "packet_json": _json(packet), "packet_sha256": _sha(packet)}


def ingest_date_check(connection, database, manifest, apply):
    from .owner_checks import _plan
    from .publication import stage_snapshot
    run = _run(manifest)
    plan = manifest["plan"]
    if plan.get("date_semantics") != DATE_SEMANTICS:
        raise ValueError("new date writes require the current typed-date plan")
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
    if manifest.get("manual_authorization"):
        receipt = _manual_receipt(snapshot)
        approve_date_check(connection, database, snapshot, receipt, True)
        return {"status": "approved", "candidate_sha256": sha, "authorization_basis": MANUAL_AUTHORIZATION}
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
    if snapshot[OVERLAY]["manifest"].get("manual_authorization"):
        _frozen_scope(snapshot)
        if receipt != _manual_receipt(snapshot):
            raise ValueError("manual date authorization must bind the exact instruction and candidate")
        return
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
        if manifest.get("manual_authorization"):
            authorization = manifest["manual_authorization"]
            date_proof = entry["field_evidence"][field]
            prior = display.get("manual_date_overrides", {}).get(field)
            if not display.get(field):
                display.update(inherited_article_date(view, field=field))
            if manifest["plan"].get("source_date_resolution") == "current-approved-version.v1":
                source_effective = inherited_article_date(view, field=field, source_only=True)
            else:
                source_effective = prior.get("source_effective") if prior else {
                    key: display.get(key) for key in (field, field + "_evidence", field + "_verification")}
            if authorization.get("action") == "revoke":
                if not prior:
                    raise ValueError("manual revocation requires an active field override")
                for key in (field, field + "_evidence", field + "_verification"):
                    display.pop(key, None)
                display.update({key: value for key, value in source_effective.items() if value is not None})
                display["manual_date_overrides"].pop(field)
                if not display["manual_date_overrides"]:
                    display.pop("manual_date_overrides")
                continue
            evidence = {"text": date_proof["text"], "source_date_precision": date_proof["precision"],
                "date_normalization": date_proof.get("date_normalization"), "authority": "human_override",
                "instruction_reference": manifest["authorization_reference"]}
            decision = {"schema_version": MANUAL_AUTHORIZATION, "actor": manifest["actor"],
                "authorized_by": authorization["authorized_by"], "instruction": authorization["instruction"],
                "instruction_reference": manifest["authorization_reference"], "audit_reference": manifest["audit_reference"],
                "executed_at": manifest["checked_at"], "field": field,
                "old_value": display.get(field), "new_value": entry["public_fields"][field],
                "scope": authorization["scope"], "content_version_id": authorization.get("content_version_id"),
                "baseline_candidate_sha256": overlay["baseline_candidate_sha256"],
                "authorization_id": digest([manifest, field]), "evidence": evidence}
            decision["source_effective"] = source_effective
            display.setdefault("manual_date_overrides", {})[field] = decision
            display.update({field: decision["new_value"], field + "_evidence": evidence,
                field + "_verification": {"verification_basis": MANUAL_AUTHORIZATION,
                    "review_scope": manifest["plan"]["scope"], "authority": "human_override",
                    "authorization_id": decision["authorization_id"]}})
            continue
        if field in display.get("manual_date_overrides", {}):
            raise ValueError("active human date override requires an explicit human replacement")
        date_proof = entry["field_evidence"][field]
        evidence = {"text": date_proof["excerpt"], "date_kind": date_proof["date_kind"],
            "source_url": entry["capture"]["requested_url"], "final_url": entry["capture"]["final_url"],
            "capture_sha256": entry["capture"]["sha256"], "source_identity": entry["source_identity"],
            "source_system": "owner_agent", "table": "registry_reviews", "record_id": proof["review_id"]}
        if manifest["plan"].get("date_semantics") == DATE_SEMANTICS:
            evidence["captured_at"] = entry["capture"]["captured_at"]
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
        if "baseline_content_version_id" in manifest["plan"]:
            date_verification["content_version_id"] = manifest["plan"]["baseline_content_version_id"]
        display.update({field: entry["public_fields"][field], field + "_evidence": evidence,
            field + "_verification": date_verification})
    # Readers apply the date after validating the inherited source's own fields.
    view["_approved_candidate_sha256"] = digest(snapshot)
    if digest(_nondate(view, fields)) != manifest["plan"]["nondate_scope_sha256"]:
        raise ValueError("date-field publication would change approved non-date facts")
    return view


def current_date_view(connection, kind, identity):
    """Visibility never revokes a retained record-level instruction."""
    row = connection.execute("SELECT c.candidate_sha256,c.snapshot_json FROM registry_publication p JOIN registry_candidates c ON c.candidate_sha256=p.published_candidate_sha256 WHERE p.entity_kind=? AND p.entity_id=?", (kind, identity)).fetchone()
    if row is None:
        return {}
    base = json.loads(row[1])
    if digest(base) != row[0]:
        raise ValueError("published date inheritance snapshot hash differs")
    reviews = [dict(zip(("review_id", "candidate_sha256", "status", "reviewed_at", "receipt_json"), review))
        for review in connection.execute("SELECT * FROM registry_reviews WHERE candidate_sha256=? AND status IN ('pass','accepted_legacy')", (row[0],))]
    if not reviews:
        raise ValueError("published date inheritance requires its exact approval receipt")
    for review in reviews:
        receipt = json.loads(review["receipt_json"])
        if review["review_id"] not in {digest([row[0], review["status"], receipt]), digest([row[0], receipt])}:
            raise ValueError("published date inheritance approval hash differs")
    from .verification import evaluate_snapshot, permitted_view
    verdict = evaluate_snapshot(base, reviews=reviews)
    return permitted_view(base, verdict) or {}


def active_manual_dates(connection, kind, identity):
    """Read authorization from the current approved version, never the pending one."""
    return deepcopy(current_date_view(connection, kind, identity).get("derived_display", {}).get("manual_date_overrides", {}))


def inherited_article_date(snapshot, *, field="publication_date", incoming=None, source_only=False):
    display = snapshot.get("derived_display", {})
    keys = (field, field + "_evidence", field + "_verification")
    if source_only:
        decision = display.get("manual_date_overrides", {}).get(field)
        display = decision["source_effective"] if decision else display
        if field != "publication_date":
            verification = display.get(field + "_verification") or {}
            if (verification.get("review_scope") == DATE_FIELDS_SCOPE and verification.get("content_version_id")
                    and applicable_manual_dates(snapshot, {field: {"scope": "content_version",
                        "content_version_id": verification["content_version_id"]}})):
                return {key: deepcopy(display[key]) for key in keys if display.get(key) is not None}
            from .article_dates import validate_content_update
            bundle = validate_content_update(snapshot)
            proof = bundle.get("date_evidence", {}).get(field) if bundle else None
            if proof:
                return {field: proof["value"], field + "_evidence": deepcopy(proof)}
            evidence = display.get(field + "_evidence") or {}
            if not (evidence.get("content_version_id") and applicable_manual_dates(snapshot, {field: {
                    "scope": "content_version", "content_version_id": evidence["content_version_id"]}})):
                return {}
            return {key: deepcopy(display[key]) for key in keys if display.get(key) is not None}
    if incoming is not None:
        decision = display.get("manual_date_overrides", {}).get(field)
        active = applicable_manual_dates(incoming, {field: decision}) if decision else {}
        if decision and not active:
            display = decision["source_effective"]
        if field != "publication_date" and not active:
            verification = display.get(field + "_verification") or {}
            if (verification.get("review_scope") != DATE_FIELDS_SCOPE or not verification.get("content_version_id")
                    or not applicable_manual_dates(incoming, {field: {"scope": "content_version",
                        "content_version_id": verification["content_version_id"]}})):
                return {}
    if display.get(field):
        return {key: deepcopy(display[key]) for key in keys if key in display}
    from .article_dates import select_article_dates
    observations = [{"kind": row["observation_kind"], "observed_at": row["observed_at"],
        "evidence": json.loads(row["evidence_json"])} for table in ("article_date_observations", "pdf_article_date_observations")
        for row in snapshot.get("tables", {}).get(table, [])]
    acquisitions = [{"publication_date": row.get("publication_date"),
        "publication_date_evidence": json.loads(row.get("publication_date_evidence_json") or "null"),
        "processing_status": row.get("processing_status"), "selection_status": row.get("selection_status")}
        for row in snapshot.get("tables", {}).get("acquisition_items", [])]
    values = select_article_dates(observations, acquisition_observations=acquisitions)
    return {key: values[key] for key in keys if values.get(key) is not None}


def applicable_manual_dates(snapshot, decisions):
    rows = snapshot.get("tables", {}).get("articles", [])
    version = next((row.get("current_content_version_id") for row in rows
        if row.get("article_id") == snapshot.get("entity_id")), None)
    if not version and snapshot.get("entity_kind") == "article" and len(rows) == 1:
        version = rows[0].get("current_content_version_id")
    if "tables" not in snapshot:
        version = snapshot.get("content_version_id") or (snapshot.get("content") or {}).get("content_version_id")
    return {field: value for field, value in decisions.items()
        if value["scope"] == "record" or version and value.get("content_version_id") == version}


def carry_manual_dates(snapshot, decisions):
    if not decisions:
        return
    display = snapshot.setdefault("derived_display", {})
    active = applicable_manual_dates(snapshot, decisions)
    for field, decision in decisions.items():
        if field == "activity_dates":
            for table in ("article_date_observations", "pdf_article_date_observations"):
                rows = snapshot.setdefault("tables", {}).setdefault(table, [])
                rows[:] = [row for row in rows
                    if json.loads(row.get("evidence_json") or "{}").get("authority") != "human_override"]
                if field in active:
                    existing_ids = {row["observation_id"] for row in rows}
                    rows.extend(deepcopy(row) for row in decision["source_effective"]["activity_observations"].get(table, [])
                        if row["observation_id"] not in existing_ids)
                    rows.extend(deepcopy(decision["activity_observations"].get(table, [])))
            events = _activity_events(snapshot)
            display.update(activity_dates=events, is_activity=bool(events))
            if field not in active:
                display.pop("activity_dates_evidence", None)
                display.pop("activity_dates_verification", None)
            continue
        if field in active:
            continue
        restored = inherited_article_date({**snapshot, "derived_display": {
            **display, "manual_date_overrides": decisions}}, field=field, incoming=snapshot)
        if (display.get(field + "_evidence") or {}).get("authority") == "human_override":
            display.pop(field, None)
            display.pop(field + "_evidence", None)
            display.update(restored)
        if (display.get(field + "_verification") or {}).get("authority") == "human_override":
            display.pop(field + "_verification", None)
    if active:
        display["manual_date_overrides"] = active
    else:
        display.pop("manual_date_overrides", None)
    for field, decision in active.items():
        display[field] = _activity_events(snapshot) if field == "activity_dates" else decision["new_value"]
        display[field + "_evidence"] = decision["evidence"]
        display[field + "_verification"] = {"verification_basis": MANUAL_AUTHORIZATION,
            "review_scope": ACTIVITY_SCOPE if field == "activity_dates" else DATE_FIELDS_SCOPE, "authority": "human_override",
            "authorization_id": decision["authorization_id"]}


def _validate_manual(manifest):
    from .article_dates import _information_date, _typed_information_date
    from .owner_checks import _nonempty
    authorization = manifest["manual_authorization"]
    if (manifest["plan"].get("scope") not in {DATE_FIELDS_SCOPE, ACTIVITY_SCOPE}
            or manifest["plan"].get("date_semantics") != DATE_SEMANTICS
            or not isinstance(authorization, dict)
            or set(authorization) - {"instruction", "authorized_by", "scope", "content_version_id", "action"}
            or authorization.get("action", "set") not in {"set", "revoke"}
            or not all(_nonempty(authorization.get(key)) for key in ("instruction", "authorized_by"))
            or authorization.get("scope") not in {"record", "content_version"}
            or authorization.get("scope") == "content_version" and not _nonempty(authorization.get("content_version_id"))
            or authorization.get("scope") == "record" and authorization.get("content_version_id") is not None):
        raise ValueError("manual override requires an explicit human instruction, actor and unambiguous date scope")
    entry = manifest["observations"][0]
    if set(entry) != {"target", "public_fields", "field_evidence"} or set(entry["field_evidence"]) != set(entry["public_fields"]):
        raise ValueError("manual override records values and instruction, not fabricated source captures")
    if manifest["plan"]["scope"] == ACTIVITY_SCOPE:
        values, proof = entry["public_fields"]["activity_dates"], entry["field_evidence"]["activity_dates"]
        if authorization.get("action") == "revoke":
            if values is not None or proof != {"text": "revoke", "precision": "unknown"}:
                raise ValueError("manual activity revocation must explicitly clear the override")
            return
        allowed = {"event_start_date", "event_end_date", "date_precision"}
        if (not isinstance(proof, dict) or set(proof) != {"fields"}
                or not isinstance(proof["fields"], list) or not proof["fields"]
                or len(proof["fields"]) != len(set(proof["fields"])) or set(proof["fields"]) - allowed):
            raise ValueError("manual activity instruction must select explicit date fields")
        validate_activity_dates(values, entry, manual=True)
        targets = manifest["plan"].get("activity_targets", [])
        seen = set()
        for value in values:
            evidence = value["evidence"]
            prior = next((event for event in targets if event["event_id"] == evidence.get("event_id")), None)
            matches = [event for event in targets if event["name"].casefold() == evidence["event_name"].casefold()
                and event["event_type"] == evidence["event_type"]]
            if (evidence.get("event_id") and not prior or not evidence.get("event_id") and matches
                    or prior and (prior["name"] != evidence["event_name"] or prior["event_type"] != evidence["event_type"])):
                raise ValueError("manual activity identity differs from its approved event")
            identity = evidence.get("event_id") or (evidence["event_name"].casefold(), evidence["event_type"])
            if identity in seen:
                raise ValueError("manual activity instruction repeats the same event")
            seen.add(identity)
            if prior:
                if evidence["event_name"] != prior["name"] or evidence.get("event_status") != prior["status"]:
                    raise ValueError("manual activity date scope cannot replace event status")
                for field, previous in (("event_start_date", "start_date"), ("event_end_date", "end_date"), ("date_precision", "date_precision")):
                    if field not in proof["fields"] and evidence.get(field) != prior[previous]:
                        raise ValueError("manual activity changed an unselected date field")
            elif set(proof["fields"]) != allowed:
                raise ValueError("new manual activity requires all explicit date bounds and precision")
        return
    for field, value in entry["public_fields"].items():
        proof = entry["field_evidence"][field]
        if not isinstance(proof, dict) or set(proof) - {"text", "precision", "date_normalization"}:
            raise ValueError("manual date precision metadata is invalid")
        precision = proof.get("precision")
        if authorization.get("action") == "revoke":
            if value is not None or proof != {"text": "revoke", "precision": "unknown"}:
                raise ValueError("manual revocation must explicitly clear the override, not invent a replacement")
            continue
        if precision == "day":
            expected = _information_date(proof.get("text"))
            if proof.get("date_normalization") is not None:
                raise ValueError("day override cannot claim month normalization")
        elif precision == "month" and proof.get("date_normalization") == "month_midpoint":
            expected = _typed_information_date(proof.get("text"), "month") + "-15"
        else:
            raise ValueError("manual date must be an exact day or explicit month midpoint")
        if value != expected:
            raise ValueError("manual target value differs from its explicit date and precision")


def _manual_receipt(snapshot):
    manifest = snapshot[OVERLAY]["manifest"]
    return {"schema_version": MANUAL_AUTHORIZATION, "decision": "authorized",
        "candidate_sha256": digest(snapshot), "source_checks": _bindings(snapshot),
        "authorization": manifest["manual_authorization"], "actor": manifest["actor"],
        "instruction_reference": manifest["authorization_reference"], "executed_at": manifest["checked_at"]}


def inventory_date_overlays(database):
    """Inventory affected frozen history without repairing or reinterpreting it."""
    import sqlite3
    from .publication import resolve_database
    database = resolve_database(database)
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        rows = []
        for sha, kind, identity, encoded in connection.execute(
                "SELECT candidate_sha256,entity_kind,entity_id,snapshot_json FROM registry_candidates ORDER BY candidate_sha256"):
            snapshot = json.loads(encoded)
            current = connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", (kind, identity)).fetchone()
            overlay = snapshot.get(OVERLAY)
            path = OVERLAY
            while overlay:
                manifest = overlay["manifest"]
                for index, entry in enumerate(manifest["observations"]):
                    proof = entry.get("field_evidence", {}).get("publication_date", {})
                    if proof.get("date_kind") not in {None, "published"}:
                        rows.append({"entity_kind": kind, "entity_id": identity, "candidate_sha256": sha,
                            "published": bool(current and current[0] == sha), "publication_date": entry["public_fields"].get("publication_date"),
                            "evidence_kind": proof.get("date_kind"), "evidence_sha256": digest(proof),
                            "evidence_path": path + ".manifest.observations[" + str(index) + "].field_evidence.publication_date", "path_kind": "date_only_overlay",
                            "disposition": "legacy_semantics_retained_requires_new_authorized_correction"})
                overlay = overlay.get("inherited_date_overlay")
                path += ".inherited_date_overlay"
            for attempt in snapshot.get("tables", {}).get("article_check_attempts", []):
                packet = json.loads(attempt.get("packet_json") or "{}")
                entry = packet.get("owner_observation", {})
                proof = entry.get("field_evidence", {}).get("publication_date", {})
                if proof.get("date_kind") not in {None, "published"}:
                    rows.append({"entity_kind": kind, "entity_id": identity, "candidate_sha256": sha,
                        "published": bool(current and current[0] == sha), "publication_date": entry.get("public_fields", {}).get("publication_date"),
                        "evidence_kind": proof["date_kind"], "evidence_sha256": digest(proof), "evidence": proof,
                        "attempt_id": attempt["attempt_id"], "path_kind": "full_owner_observation",
                        "evidence_path": "tables.article_check_attempts[" + attempt["attempt_id"] + "].packet_json.owner_observation.field_evidence.publication_date",
                        "disposition": "legacy_semantics_retained_requires_new_authorized_correction"})
        return {"schema_version": "article-date-overlay-inventory.v1", "records": rows, "count": len(rows), "write_performed": False}


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
    date_keys.update({"manual_date_overrides", "content_date", "content_date_basis", "content_date_label",
        "content_date_evidence", "content_date_authority", "content_date_precision", "content_date_normalization", "content_update_candidate_sha256"})

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
