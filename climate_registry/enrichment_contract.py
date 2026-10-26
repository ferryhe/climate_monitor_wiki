"""Versioned projection of existing frozen candidates, not a second evidence store."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from copy import deepcopy

from .acquisition_review import digest

CONTRACT_VERSION = "registry-enrichment-contract.v1"
SCOPE_VERSION = "registry-enrichment-scope.v1"
CONTENT_SCOPE = "complete_article_content_update"
INITIAL_SCOPE = "initial_article_content"
BINDING = "enrichment_scope"
DATE_PROOF_FIELDS = tuple(name + suffix for name in ("publication_date", "updated_date", "revision_date", "activity_dates")
    for suffix in ("_evidence", "_verification"))
SOURCES = ("site", "search", "pdf")
SCOPES = ("native_source_fields", "owner_source_fields.v1", "owner_source_fields.v2",
    "article_publication_date_only", "article_date_fields_only", "article_activity_dates_only",
    "human_article_dates", "article_identity_relations.v1", CONTENT_SCOPE, INITIAL_SCOPE)


def capability(source, kind, scope):
    """Capabilities of a frozen Registry input; acquisition itself stays upstream."""
    article = kind in {"article", "pdf_article"}
    supported = source in SOURCES and kind in {"article", "pdf_article", "meeting", "pdf_meeting"} and scope in SCOPES
    reason = None
    required, optional = [], []
    if scope == "article_identity_relations.v1":
        required = ["exact_frozen_participants", "source_body_occurrence_scope", "semantic_evidence", "new_independent_identity_review"]
        optional = ["approved_factual_baseline_or_pending_disposition_only", "canonical_url", "same_object_aliases", "exact_body_event_relations", "reviewed_disposition"]
    elif scope in {CONTENT_SCOPE, INITIAL_SCOPE}:
        required = (["content_update", "summary", "published_content_baseline", "approval_inheritance_baseline"] if scope == CONTENT_SCOPE
            else ["current_body", "current_version_summary_evidence", "approval_inheritance_baseline"])
        optional = ["publication_date", "updated_date", "revision_date", "applicable_human_authorization", "editorial_metadata"]
        if kind != "article":
            supported, reason = False, "content_update_requires_canonical_article_and_retained_body_versions"
    elif scope in {"article_publication_date_only", "article_date_fields_only", "article_activity_dates_only", "human_article_dates"}:
        required = ["approved_baseline", "selected_date_fields", "exact_target"]
        optional = ["month_midpoint", "content_version_scope"]
        if not article:
            supported, reason = False, "article_date_scope_requires_article_entity"
    elif scope == "owner_source_fields.v2":
        required = ["capture", "title", "summary", "original_comparisons", "excluded_fields", "taxonomy", "editorial_metadata_or_explicit_null"]
        optional = ["publication_date", "categories", "keywords"]
        if not article:
            supported, reason = False, "editorial_scope_requires_article_entity"
    elif scope == "owner_source_fields.v1":
        required = ["capture", "original_comparisons", "excluded_fields"] + (["title", "summary"] if article else ["name", "date_precision", "start_date_or_deadline"])
        optional = ["publication_date"] if article else ["organizer", "location", "timezone"]
    else:
        required = ["frozen_source_targets", "native_information_checks", "exact_candidate_review"]
    if not supported and reason is None:
        reason = "unavailable_source_entity_or_scope"
    status = "supported" if supported else "unsupported"
    if supported and source in {"site", "search"} and kind.startswith("pdf_"):
        status, reason = "unsupported", "acquisition_adapter_does_not_emit_pdf_entities"
    elif supported and source == "pdf" and kind in {"article", "meeting"}:
        status, reason = "conditional", "requires_existing_stored_canonical_link_and_native_evidence"
    return {"source": source, "entity_kind": kind, "scope": scope, "status": status,
        "reason": reason, "required": required, "optional": optional,
        "evidence": ("article-content-update.v1 plus exact published body and candidate references" if scope == CONTENT_SCOPE
            else "human-article-date-authorization.v1; no source verdict" if scope == "human_article_dates"
            else "frozen source/capture and exact independent review; date-only inherits approved unrelated fields"),
        "adapter": "climate_registry.pdf_intake" if source == "pdf" else "climate_registry.acquisition" if source in {"site", "search"} else None,
        "identity_rule": "PDF-to-Article evidence requires stored core_article_id; URL equality is insufficient"}


def capability_matrix():
    return {"schema_version": CONTRACT_VERSION, "capabilities": [capability(source, kind, scope)
        for source in SOURCES for kind in ("article", "pdf_article", "meeting", "pdf_meeting") for scope in SCOPES]}


def projected_date_proofs(display):
    """G3 null means unknown: omit it for existing date consumers, without changing the candidate."""
    return {key: value for key, value in display.items() if key not in DATE_PROOF_FIELDS or value is not None}


def _body(snapshot):
    from .identity import is_identity_scope, OVERLAY as IDENTITY
    if is_identity_scope(snapshot) and snapshot[IDENTITY]["manifest"]["plan"]["participants"][0]["expected_state"]["published_candidate_sha256"] is None:
        return None  # A disposition receipt does not approve its retained body.
    if snapshot.get("entity_kind") != "article":
        return None
    article = next((row for row in snapshot.get("tables", {}).get("articles", [])
        if row.get("article_id") == snapshot["entity_id"]), {})
    version = article.get("current_content_version_id")
    row = next((row for row in snapshot.get("tables", {}).get("article_content_versions", [])
        if row.get("article_id") == snapshot["entity_id"] and row.get("content_version_id") == version), None)
    return {"content_version_id": version, "body_sha256": hashlib.sha256(row["markdown_content"].encode()).hexdigest()} if row else None


def _initial_summary(snapshot):
    body = _body(snapshot)
    summary = snapshot.get("derived_display", {}).get("summary")
    return body and summary and any(row.get("content_version_id") == body["content_version_id"]
        and row.get("status") == "complete" and row.get("summary") == summary
        for row in snapshot.get("tables", {}).get("article_enrichments", []))


def _candidate_reviews(connection, sha):
    reviews = [dict(zip(("review_id", "candidate_sha256", "status", "receipt_json"), row)) for row in connection.execute(
        "SELECT review_id,candidate_sha256,status,receipt_json FROM registry_reviews WHERE candidate_sha256=? AND status IN ('pass','accepted_legacy')", (sha,))]
    return approval_context(connection, _candidate(connection, sha), reviews)


def approval_context(connection, snapshot, reviews, seen=()):
    """Resolve immutable approval references in this read transaction, outside receipts."""
    baseline = snapshot.get(BINDING, {}).get("approval_inheritance_baseline", {})
    if not snapshot.get(BINDING, {}).get("operation_scope"):
        return reviews
    context = {}
    for sha in {baseline.get("candidate_sha256"), baseline.get("content_review", {}).get("candidate_sha256")} - {None}:
        if sha in seen or sha == digest(snapshot):
            raise ValueError("cyclic content approval inheritance")
        source = _candidate(connection, sha)
        rows = [dict(zip(("review_id", "candidate_sha256", "status", "receipt_json"), row)) for row in connection.execute(
            "SELECT review_id,candidate_sha256,status,receipt_json FROM registry_reviews WHERE candidate_sha256=? AND status='pass'", (sha,))]
        context[sha] = {"snapshot": source, "reviews": approval_context(connection, source, rows, (*seen, digest(snapshot)))}
    return [{**review, "_approval_context": context} for review in reviews]


def inherited_content_scope(snapshot):
    return snapshot.get(BINDING, {}).get("operation_scope") in {
        "native_source_fields", "owner_source_fields.v1", "owner_source_fields.v2", "approved-public-category-only"}


def _content_signature(snapshot):
    from .article_dates import validate_content_update
    return {**(_body(snapshot) or {}), "summary_sha256": digest(snapshot.get("derived_display", {}).get("summary")),
        "content_update_sha256": digest(validate_content_update(snapshot))}


def _content_reference(proof, origin):
    reference = {key: proof[key] for key in ("candidate_sha256", "enrichment_contract_sha256", "scope", "review_id")}
    reference.update(**_body(origin), summary_sha256=hashlib.sha256(origin["derived_display"]["summary"].encode()).hexdigest(),
        content_update_sha256=_content_signature(origin)["content_update_sha256"], binding=deepcopy(origin[BINDING]),
        display={key: deepcopy(origin["derived_display"][key]) for key in ("categories", "metadata_provenance") if key in origin["derived_display"]})
    return reference


def _matches_content_reference(snapshot, origin, reference):
    return (all(reference.get(key) == value for key, value in _content_reference(
        {key: reference[key] for key in ("candidate_sha256", "enrichment_contract_sha256", "scope", "review_id")}, origin).items())
        and _content_signature(snapshot) == _content_signature(origin)
        and (origin[BINDING]["scope"] != INITIAL_SCOPE or _initial_summary(snapshot))
        and all(snapshot[BINDING].get(key) == origin[BINDING].get(key) for key in
            ("schema_version", "scope", "capability", "content_comparison_baseline", "native_taxonomies")))


def bind_existing_content(connection, snapshot, *, operation_scope=None):
    """Carry a real content approval into a separately reviewed, unchanged-body operation."""
    from .owner_dates import OVERLAY, current_date_view
    sha, published = _published(connection, snapshot)
    if not published or BINDING not in published:
        return False
    if digest(snapshot) == sha:
        return True
    accepted = _content_review(published, _candidate_reviews(connection, sha))
    if not accepted or _body(snapshot) != _body(accepted[1]):
        return False
    proof, origin, _ = accepted
    approved = current_date_view(connection, snapshot["entity_kind"], snapshot["entity_id"])
    display = snapshot.setdefault("derived_display", {})
    public = approved.get("derived_display", {})
    previous = published.get("derived_display", {})
    for field in ("categories", "keywords"):
        if operation_scope == "approved-public-category-only" and field == "categories":
            continue
        if field in public and (field not in display or (display[field] == previous.get(field)
                and display.get("metadata_provenance", {}).get(field) == previous.get("metadata_provenance", {}).get(field))):
            display[field] = deepcopy(public[field])
            provenance = public.get("metadata_provenance", {}).get(field)
            if provenance is not None:
                display.setdefault("metadata_provenance", {})[field] = provenance
    for key in ("summary", "content_update"):
        if key not in display and key in origin.get("derived_display", {}):
            display[key] = deepcopy(origin["derived_display"][key])
    snapshot[BINDING] = deepcopy(origin[BINDING])
    if (_content_signature(snapshot) != _content_signature(origin)
            or origin[BINDING]["scope"] == INITIAL_SCOPE and not _initial_summary(snapshot)):
        raise ValueError("inherited content body, version, summary or update package differs")
    if operation_scope is None:
        from .owner_checks import review_bindings, EDITORIAL_CHECK_VERSION
        bindings = review_bindings(snapshot)
        operation_scope = ("owner_source_fields.v2" if any(row.get("check_schema_version") == EDITORIAL_CHECK_VERSION for row in bindings)
            else "owner_source_fields.v1" if bindings else "native_source_fields")
    snapshot.pop(OVERLAY, None)
    date_keys = {"manual_date_overrides", "activity_dates", "is_activity", "activity_dates_evidence", "activity_dates_verification"}
    date_keys.update(field + suffix for field in ("publication_date", "updated_date", "revision_date") for suffix in ("", "_evidence", "_verification"))
    for key in date_keys:
        display.pop(key, None)
        if key in approved.get("derived_display", {}):
            display[key] = deepcopy(approved["derived_display"][key])
    snapshot[BINDING].update(operation_scope=operation_scope,
        approval_inheritance_baseline={**_approval_baseline(connection, snapshot, sha), "content_review": _content_reference(proof, origin)})
    return True


def _editorial_inheritance(connection, snapshot, sha):
    if not sha:
        return {}
    from .owner_dates import _baseline_view
    baseline = _candidate(connection, sha)
    if (baseline["entity_kind"], baseline["entity_id"]) != (snapshot["entity_kind"], snapshot["entity_id"]):
        raise ValueError("editorial approval inheritance identity differs")
    reviews = _candidate_reviews(connection, sha)
    from .identity import is_identity_scope, verdict as identity_verdict
    if is_identity_scope(baseline) and identity_verdict(baseline, reviews).get("disposition_only"):
        return {}
    approved, _ = _baseline_view(baseline, reviews)
    approved = approved.get("derived_display", {})
    display, inherited = snapshot.get("derived_display", {}), {}
    content = _content_review(baseline, reviews)
    native_values = None
    for field in ("categories", "keywords"):
        provenance = approved.get("metadata_provenance", {}).get(field)
        key = ("editorial_metadata_provenance" if provenance == "owner_reviewed_editorial_classification"
            else "category_correction_provenance" if field == "categories" and provenance == "reviewed_category_correction" else None)
        authority = {key: approved[key]} if key and approved.get(key) else None
        if provenance == "source_report" and content:
            proof, origin, _ = content
            if native_values is None:
                native_values = list(_native_editorial_values(connection, origin))
            original = origin.get("derived_display", {})
            if (original.get("metadata_provenance", {}).get(field) == provenance
                    and original.get(field) == approved.get(field)
                    and any(value[field] == original[field] for value in native_values)):
                authority = {"content_review": {name: proof[name] for name in
                    ("candidate_sha256", "enrichment_contract_sha256", "scope", "review_id")},
                    "native_taxonomy_bindings": review_projection(origin)["native_taxonomy_bindings"]}
        if (authority and display.get(field) == approved.get(field)
                and display.get("metadata_provenance", {}).get(field) == provenance):
            inherited[field] = {"sha256": digest([approved[field], provenance]),
                "authority": authority}
    return inherited


def _approval_baseline(connection, snapshot, sha):
    baseline = {"candidate_sha256": sha}
    editorial = _editorial_inheritance(connection, snapshot, sha)
    if editorial:
        baseline["editorial"] = editorial
    return baseline


def inherited_editorial_projection(snapshot):
    """Only fields frozen against an approved baseline may survive owner cleanup."""
    display, result = snapshot.get("derived_display", {}), {}
    inherited = snapshot.get(BINDING, {}).get("approval_inheritance_baseline", {}).get("editorial", {})
    for field in ("categories", "keywords"):
        proof = inherited.get(field)
        provenance = display.get("metadata_provenance", {}).get(field)
        if proof and proof.get("sha256") == digest([display.get(field), provenance]):
            result[field] = display[field]
            result.setdefault("metadata_provenance", {})[field] = provenance
            for key in ("editorial_metadata_provenance", "category_correction_provenance"):
                if key in proof["authority"]:
                    result[key] = proof["authority"][key]
    return result


def _native_editorial_values(connection, snapshot, *, retain_taxonomies=None):
    """Use the existing validated semantic-import rows, not an invented editorial input."""
    from climate_monitor.taxonomy import load_article_taxonomy, frozen_article_taxonomy, validate_semantic_bundle
    from .semantic_import import _bundle_sha256
    article = next(row for row in snapshot["tables"]["articles"] if row["article_id"] == snapshot["entity_id"])
    for row in snapshot["tables"].get("article_semantics", []):
        if row.get("article_id") != snapshot["entity_id"] or row.get("canonical_url") != article["canonical_url"]:
            continue
        cursor = connection.execute("SELECT * FROM article_semantics WHERE report_id=? AND article_id=?", (row["report_id"], row["article_id"]))
        stored = cursor.fetchone()
        if stored is None or dict(zip((column[0] for column in cursor.description), stored)) != row:
            raise ValueError("native editorial evidence differs from its stored semantic import")
        if (not any(report.get("report_id") == row["report_id"] and report.get("report_sha256") == row["report_sha256"]
                for report in snapshot.get("dependencies", {}).get("reports", []))
                or not any(item.get("report_id") == row["report_id"] and item.get("article_id") == row["article_id"]
                    for item in snapshot["tables"].get("report_appearances", []))):
            raise ValueError("native editorial evidence lacks its exact frozen report binding")
        identity = {"taxonomy_id": row.get("taxonomy_id"), "sha256": row.get("taxonomy_raw_sha256")}
        retained = next((value for value in snapshot.get(BINDING, {}).get("native_taxonomies", [])
            if all(value.get(key) == expected for key, expected in identity.items())), None)
        if retained:
            taxonomy = frozen_article_taxonomy(retained)
        else:
            taxonomy = load_article_taxonomy()
            if (identity["taxonomy_id"], identity["sha256"]) != (taxonomy.taxonomy_id, taxonomy.sha256):
                taxonomy = frozen_article_taxonomy(identity)
        bundle = validate_semantic_bundle({"schema_version": "article-semantic-bundle.v1", "taxonomy_id": taxonomy.taxonomy_id,
            "taxonomy_sha256": taxonomy.sha256, "summary": row["summary"],
            "categories": json.loads(row["categories_json"]), "keywords": json.loads(row["keywords_json"])}, taxonomy=taxonomy)
        if row.get("bundle_sha256") != _bundle_sha256(bundle):
            raise ValueError("native editorial semantic bundle hash differs")
        if retain_taxonomies is not None:
            evidence = {**identity, "definition_yaml": taxonomy.definition_yaml}
            if evidence not in retain_taxonomies:
                retain_taxonomies.append(evidence)
        yield bundle


def _validate_optional_fields(connection, snapshot, *, retain_taxonomies=None):
    """G3 structure and authority checks; neither is a semantic PASS."""
    from climate_monitor.taxonomy import _semantic_values
    from .owner_dates import current_date_view
    from climate_monitor.taxonomy import frozen_article_taxonomy
    native_taxonomies = snapshot.get(BINDING, {}).get("native_taxonomies", [])
    if (not isinstance(native_taxonomies, list) or any(not isinstance(value, dict)
            or set(value) != {"taxonomy_id", "sha256", "definition_yaml"} for value in native_taxonomies)):
        raise ValueError("native taxonomy evidence must retain its exact identity and definition")
    for identity in native_taxonomies:
        frozen_article_taxonomy(identity)
        if not any(row.get("taxonomy_id") == identity["taxonomy_id"] and row.get("taxonomy_raw_sha256") == identity["sha256"]
                for row in snapshot["tables"].get("article_semantics", [])):
            raise ValueError("native taxonomy evidence lacks its frozen semantic-import binding")
    display = snapshot.get("derived_display", {})
    for field in DATE_PROOF_FIELDS:
        if display.get(field) is not None and not isinstance(display[field], dict):
            raise ValueError(field + " must be a mapping or null")
    provenance = display.get("metadata_provenance", {})
    if (not isinstance(provenance, dict) or set(provenance) - {"categories", "keywords"}
            or any(value is not None and not isinstance(value, str) for value in provenance.values())):
        raise ValueError("metadata_provenance must map supported editorial fields to strings or null")
    for field in ("categories", "keywords"):
        if field in display:
            value = display[field]
            if not isinstance(value, list):
                raise ValueError(field + " must be a list of strings")
            _semantic_values(value, name=field, minimum=0, maximum=len(value), item_maximum=None)
    if "is_activity" in display and type(display["is_activity"]) is not bool:
        raise ValueError("is_activity must be a boolean")
    if "activity_dates" in display and (not isinstance(display["activity_dates"], list)
            or any(not isinstance(row, dict) for row in display["activity_dates"])):
        raise ValueError("activity_dates must be a list of existing approved activity records")
    if not ({"categories", "keywords", "is_activity", "activity_dates", "activity_dates_evidence", "activity_dates_verification"} & display.keys()):
        return
    approved = current_date_view(connection, snapshot["entity_kind"], snapshot["entity_id"]).get("derived_display", {})
    published_sha, previous = _published(connection, snapshot)
    previous_display = (previous or {}).get("derived_display", {})
    from .owner_checks import review_bindings
    bound = {row["attempt_id"] for row in review_bindings(snapshot)}
    owner_values = []
    for row in snapshot["tables"].get("article_check_attempts", []):
        if row["attempt_id"] in bound:
            packet = json.loads(row["packet_json"])
            if packet.get("editorial_metadata") is not None:
                cursor = connection.execute("SELECT * FROM article_check_attempts WHERE attempt_id=?", (row["attempt_id"],))
                stored = cursor.fetchone()
                if stored is None or dict(zip((column[0] for column in cursor.description), stored)) != row:
                    raise ValueError("owner editorial evidence differs from its stored immutable capture")
                owner_values.append(packet["editorial_metadata"])
    native_values = None
    exact_published = (digest(snapshot) == published_sha
        and bool(_content_review(snapshot, _candidate_reviews(connection, published_sha))))
    if (retain_taxonomies is not None and not inherited_content_scope(snapshot) and not exact_published
            and "source_report" in provenance.values()):
        native_values = list(_native_editorial_values(connection, snapshot, retain_taxonomies=retain_taxonomies))
    for field in ("categories", "keywords"):
        if field not in display:
            continue
        value = display[field]
        if exact_published:
            continue  # The unchanged frozen proposal already has its exact content and operation approvals.
        # Previously approved arrays retain their frozen taxonomy/authority, even after a taxonomy change.
        if field in approved and value == approved[field] and provenance.get(field) == approved.get("metadata_provenance", {}).get(field):
            continue
        # Source-only owner cleanup still removes unchanged, retained unreviewed classifier fields.
        if (bound and not owner_values and field in previous_display and value == previous_display[field]
                and provenance.get(field) == previous_display.get("metadata_provenance", {}).get(field)):
            continue
        if any(value == metadata[field] for metadata in owner_values):
            continue  # review_bindings already verifies v2 taxonomy, literal capture and field evidence.
        if native_values is None:
            native_values = list(_native_editorial_values(connection, snapshot, retain_taxonomies=retain_taxonomies))
        if any(value == metadata[field] for metadata in native_values):
            continue
        raise ValueError("required_missing: " + field + " exact frozen editorial/taxonomy evidence or approved inheritance")
    for field in ("is_activity", "activity_dates", "activity_dates_evidence", "activity_dates_verification"):
        if field not in display:
            continue
        default = False if field == "is_activity" else [] if field == "activity_dates" else None
        if display[field] == approved.get(field, default):
            continue
        # Activity values use a different existing schema from the public DTO. Its writer/approved
        # baseline validates that schema; G3 cannot introduce an unbound DTO as source evidence.
        raise ValueError(field + " requires the existing approved activity-date scope")


def _validate_update_captures(snapshot, bundle):
    from .article_dates import _collection_timestamp
    fetches = [row for row in snapshot.get("tables", {}).get("article_fetches", [])
        if row.get("article_id") == snapshot["entity_id"] and row.get("content_version_id") == bundle["content_version_id"]
        and row.get("fetch_status") == "success" and 200 <= (row.get("http_status") or 0) <= 299]
    for proof in bundle["date_evidence"].values():
        if not any(row.get("final_url") == proof["final_url"] and
                _collection_timestamp(row["fetched_at"]) == _collection_timestamp(proof["captured_at"]) for row in fetches):
            raise ValueError("content date capture does not match a successful fetch of the exact current body")


def _validate_scope_dates(connection, snapshot, bundle):
    from .article_dates import validate_content_date_evidence
    from .owner_dates import current_date_view, inherited_article_date
    display = snapshot.get("derived_display", {})
    approved = current_date_view(connection, snapshot["entity_kind"], snapshot["entity_id"])
    article = next((row for row in snapshot.get("tables", {}).get("articles", [])
        if row.get("article_id") == snapshot["entity_id"]), {})
    version = article.get("current_content_version_id")
    current = next((row for row in snapshot.get("tables", {}).get("article_content_versions", [])
        if row.get("article_id") == snapshot["entity_id"] and row.get("content_version_id") == version), None)
    for field in ("publication_date", "updated_date", "revision_date"):
        value = display.get(field)
        if value is None:
            continue
        inherited = inherited_article_date(approved, field=field, incoming=snapshot)
        if inherited.get(field) == value and all(display.get(field + suffix) is None or
                display[field + suffix] == inherited.get(field + suffix) for suffix in ("_evidence", "_verification")):
            continue
        proof = (bundle or {}).get("date_evidence", {}).get(field)
        if proof and value == proof["value"] and display.get(field + "_evidence", proof) == proof and not display.get(field + "_verification"):
            continue
        # Updated/Revision facts for a new body belong in its one complete package.
        if current and (field == "publication_date" or bundle is None):
            proof = display.get(field + "_evidence")
            if isinstance(proof, dict) and value == proof.get("value") and not display.get(field + "_verification"):
                validate_content_date_evidence(field, proof, article=article, current=current, version=version)
                _validate_update_captures(snapshot, {"content_version_id": version, "date_evidence": {field: proof}})
                continue
        raise ValueError("content scope date lacks bound evidence or valid approved inheritance: " + field)


def _candidate(connection, sha):
    row = connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()
    if not row or digest(json.loads(row[0])) != sha:
        raise ValueError("enrichment baseline candidate is missing or its hash differs")
    return json.loads(row[0])


def _published(connection, snapshot):
    row = connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?",
        (snapshot["entity_kind"], snapshot["entity_id"])).fetchone()
    sha = row[0] if row else None
    return sha, _candidate(connection, sha) if sha else None


def _content_origin(snapshot):
    # Corrections change the approval baseline, never the body comparison origin.
    from .owner_dates import OVERLAY, _base
    from .identity import is_identity_scope, OVERLAY as IDENTITY
    while True:
        if is_identity_scope(snapshot):
            snapshot = snapshot[IDENTITY]["manifest"]["plan"]["participants"][0]["snapshot"]
        elif OVERLAY in snapshot:
            snapshot, _ = _base(snapshot)
        else:
            return snapshot


def bind_content_scope(connection, snapshot):
    """Freeze scope, the two approval references and required native taxonomy source bytes."""
    from .article_dates import validate_content_update
    from .owner_dates import OVERLAY
    if OVERLAY in snapshot or BINDING not in snapshot:
        return
    supplied = snapshot[BINDING]
    if (not isinstance(supplied, dict) or supplied.get("schema_version") != SCOPE_VERSION
            or supplied.get("scope") not in {CONTENT_SCOPE, INITIAL_SCOPE}
            or set(supplied) - {"schema_version", "scope", "capability", "content_comparison_baseline", "approval_inheritance_baseline", "operation_scope", "native_taxonomies"}
            or "operation_scope" in supplied and not inherited_content_scope(snapshot)):
        raise ValueError("unsupported enrichment scope")
    scope = supplied["scope"]
    allowed = {"summary", "content_update", "categories", "keywords", "metadata_provenance", "manual_date_overrides",
        "activity_dates", "is_activity", "activity_dates_evidence", "activity_dates_verification"}
    for field in ("publication_date", "updated_date", "revision_date"):
        allowed.update((field, field + "_evidence", field + "_verification"))
    if set(snapshot.get("derived_display", {})) - allowed:
        raise ValueError("unsupported public field in declared enrichment scope")
    native_taxonomies = deepcopy(supplied.get("native_taxonomies", []))
    _validate_optional_fields(connection, snapshot, retain_taxonomies=native_taxonomies)
    native_evidence = {"native_taxonomies": sorted(native_taxonomies, key=lambda value: (value["taxonomy_id"], value["sha256"]))} if native_taxonomies else {}
    if inherited_content_scope(snapshot):
        validate_content_baselines(connection, snapshot, digest(snapshot))
        return
    bundle = validate_content_update(snapshot)
    if snapshot.get("entity_kind") != "article":
        raise ValueError("content_update_requires_canonical_article_and_retained_body_versions")
    published_sha, published = _published(connection, snapshot)
    display = snapshot.get("derived_display", {})
    if display.get("manual_date_overrides") or any((display.get(field + suffix) or {}).get("authority") == "human_override"
            for field in ("publication_date", "updated_date", "revision_date") for suffix in ("_evidence", "_verification")):
        from .owner_dates import current_date_view, applicable_manual_dates
        approved = current_date_view(connection, snapshot["entity_kind"], snapshot["entity_id"])
        active = applicable_manual_dates(snapshot, approved.get("derived_display", {}).get("manual_date_overrides", {}))
        if any(active.get(field) != decision for field, decision in display.get("manual_date_overrides", {}).items()):
            raise ValueError("content scope cannot introduce or rebind a human authorization")
        if any(field not in active and any((display.get(field + suffix) or {}).get("authority") == "human_override"
                for suffix in ("_evidence", "_verification")) for field in ("publication_date", "updated_date", "revision_date")):
            raise ValueError("content scope human date authority has no applicable approved instruction")
        approved_display = approved.get("derived_display", {})
        for field in ("publication_date", "updated_date", "revision_date"):
            for suffix in ("_evidence", "_verification"):
                proof = display.get(field + suffix) or {}
                if proof.get("authority") == "human_override" and (display.get(field) != active[field]["new_value"]
                        or proof != approved_display.get(field + suffix)):
                    raise ValueError("content scope human date value or authority reference differs from the approved instruction")
    _validate_scope_dates(connection, snapshot, bundle)
    if scope == INITIAL_SCOPE:
        if bundle:
            raise ValueError("initial scope cannot assert a content update or update note")
        if not _initial_summary(snapshot):
            raise ValueError("required_missing: initial current body and version-bound summary evidence")
        if published and _body(published):
            if digest({key: value for key, value in snapshot.items() if key != BINDING}) == digest(
                    {key: value for key, value in published.items() if key != BINDING}) and published.get(BINDING, {}).get("scope") == INITIAL_SCOPE:
                snapshot[BINDING] = published[BINDING]
                return
            raise ValueError("initial scope cannot replace an already published body")
        snapshot[BINDING] = {"schema_version": SCOPE_VERSION, "scope": INITIAL_SCOPE,
            "capability": "current_content_version_and_enrichment",
            "content_comparison_baseline": {"status": "not_applicable", "reason": "initial_content_publication"},
            "approval_inheritance_baseline": _approval_baseline(connection, snapshot, published_sha), **native_evidence}
        return
    if not bundle:
        raise ValueError("content scope requires its complete content update package")
    _validate_update_captures(snapshot, bundle)
    if not published or not _body(published):
        raise ValueError("required_missing: published_content_baseline")
    current = _body(published)
    expected = {"content_version_id": bundle["previous_content_version_id"], "body_sha256": bundle["previous_body_sha256"]}
    origin = _content_origin(published)
    if digest({key: value for key, value in snapshot.items() if key != BINDING}) == digest(
            {key: value for key, value in published.items() if key != BINDING}) and BINDING in published:
        snapshot[BINDING] = published[BINDING]
        return
    if current == _body(snapshot):
        prior = origin.get(BINDING, {}).get("content_comparison_baseline")
        if not prior or {key: prior[key] for key in expected} != expected:
            raise ValueError("content comparison baseline mismatch; reconcile against current published content")
        comparison = prior
    elif current == expected:
        comparison = {"candidate_sha256": digest(origin), **current}
    else:
        raise ValueError("content comparison baseline mismatch; reconcile against current published content")
    snapshot[BINDING] = {"schema_version": SCOPE_VERSION, "scope": CONTENT_SCOPE,
        "capability": "article-content-update.v1", "content_comparison_baseline": comparison,
        "approval_inheritance_baseline": _approval_baseline(connection, snapshot, published_sha), **native_evidence}


def validate_content_baselines(connection, snapshot, sha):
    """Approval and re-stage both reject stale substantive bodies, without sorting hashes."""
    from .owner_dates import OVERLAY
    if OVERLAY in snapshot or BINDING not in snapshot:
        return  # Historical candidates keep their frozen schema and scope.
    _validate_optional_fields(connection, snapshot)
    if inherited_content_scope(snapshot):
        baseline = snapshot[BINDING]["approval_inheritance_baseline"]
        published_sha, _ = _published(connection, snapshot)
        if published_sha not in {sha, baseline["candidate_sha256"]}:
            raise ValueError("approval inheritance baseline mismatch; stage against current publication")
        reference = baseline["content_review"]
        origin = _candidate(connection, reference["candidate_sha256"])
        accepted = _content_review(origin, _candidate_reviews(connection, reference["candidate_sha256"]))
        parent = _candidate(connection, baseline["candidate_sha256"])
        parent_approval = _content_review(parent, _candidate_reviews(connection, baseline["candidate_sha256"]))
        if (not accepted or not parent_approval or any(accepted[0][key] != reference[key] or parent_approval[0][key] != reference[key]
                for key in ("candidate_sha256", "enrichment_contract_sha256", "scope", "review_id"))
                or not _matches_content_reference(snapshot, origin, reference)):
            raise ValueError("inherited content approval or unchanged content binding differs")
        if baseline.get("editorial", {}) != _editorial_inheritance(connection, snapshot, baseline["candidate_sha256"]):
            raise ValueError("editorial approval inheritance differs from its exact approved baseline")
        from .article_dates import validate_content_update
        _validate_scope_dates(connection, snapshot, validate_content_update(snapshot))
        return
    binding = snapshot[BINDING]
    if binding.get("schema_version") != SCOPE_VERSION or binding.get("scope") not in {CONTENT_SCOPE, INITIAL_SCOPE}:
        raise ValueError("unsupported enrichment scope")
    from .article_dates import validate_content_update
    bundle = validate_content_update(snapshot)
    _validate_scope_dates(connection, snapshot, bundle)
    comparison = binding["content_comparison_baseline"]
    if binding["scope"] == CONTENT_SCOPE:
        if not bundle or binding.get("capability") != "article-content-update.v1":
            raise ValueError("content scope requires its complete content update package")
        _validate_update_captures(snapshot, bundle)
        previous = _candidate(connection, comparison["candidate_sha256"])
        expected = {"content_version_id": bundle["previous_content_version_id"], "body_sha256": bundle["previous_body_sha256"]}
        if ((previous["entity_kind"], previous["entity_id"]) != (snapshot["entity_kind"], snapshot["entity_id"])
                or _body(previous) != expected or {key: comparison.get(key) for key in expected} != expected):
            raise ValueError("content comparison baseline mismatch")
    elif (bundle or binding.get("capability") != "current_content_version_and_enrichment" or not _initial_summary(snapshot)
            or comparison != {"status": "not_applicable", "reason": "initial_content_publication"}):
        raise ValueError("initial scope comparison must be not_applicable")
    published_sha, _ = _published(connection, snapshot)
    if published_sha not in {sha, binding["approval_inheritance_baseline"]["candidate_sha256"]}:
        raise ValueError("approval inheritance baseline mismatch; stage against current publication")
    baseline = binding["approval_inheritance_baseline"]
    if baseline.get("editorial", {}) != _editorial_inheritance(connection, snapshot, baseline["candidate_sha256"]):
        raise ValueError("editorial approval inheritance differs from its exact approved baseline")


def review_projection(snapshot):
    """Derive the same handoff for authors, reviewers and downstream readers."""
    from .owner_dates import OVERLAY
    from .article_dates import validate_content_update, content_date_projection
    from .identity import is_identity_scope
    if is_identity_scope(snapshot):
        from .identity import _frozen, _scope
        _, _, manifest = _frozen(snapshot)
        return {"schema_version": CONTRACT_VERSION, "scope": "article_identity_relations.v1",
            "canonical": {"entity_kind": snapshot["entity_kind"], "entity_id": snapshot["entity_id"]},
            "participants": [_scope(ref) for ref in manifest["plan"]["participants"]],
            "aliases": manifest["aliases"], "relations": manifest["relations"], "disposition": manifest["disposition"],
            "source_evidence_sha256": digest(manifest["evidence"]), "content_generation": ("unchanged_approved_content" if manifest["plan"]["participants"][0]["expected_state"]["published_candidate_sha256"] else "not_applicable_disposition_only"),
            "next_owners": ["G6", "G11", "G12b"]}
    original = snapshot
    overlay = snapshot.get(OVERLAY)
    binding = snapshot.get(BINDING)
    entries, taxonomies, plans = [], [], []
    if overlay:
        manifest = overlay["manifest"]
        entries = manifest["observations"]
        plans = [manifest["plan"]]
        scope = "human_article_dates" if manifest.get("manual_authorization") else manifest["plan"]["scope"]
        inheritance = {"candidate_sha256": overlay["baseline_candidate_sha256"],
            "public_projection_sha256": manifest["plan"]["baseline_public_projection_sha256"],
            "reviews": [{key: row[key] for key in ("candidate_sha256", "review_id", "status")} for row in manifest["plan"]["baseline_reviews"]]}
    else:
        from .owner_checks import review_bindings
        bound_attempts = {row["attempt_id"] for row in review_bindings(snapshot)}
        for prefix in ("article", "meeting"):
            for row in snapshot.get("tables", {}).get(prefix + "_check_attempts", []):
                if row["attempt_id"] not in bound_attempts:
                    continue
                packet = json.loads(row["packet_json"])
                entries.append(packet["owner_observation"])
                plans.append(packet["baseline"])
                if packet.get("taxonomy"):
                    taxonomies.append(packet["taxonomy"])
        scope = binding.get("operation_scope", binding["scope"]) if binding else "owner_source_fields.v2" if taxonomies else "owner_source_fields.v1" if entries else "native_source_fields"
        inheritance = binding["approval_inheritance_baseline"] if binding else {
            "candidate_sha256": plans[-1]["expected_state"]["published_candidate_sha256"] if plans else None,
            "status": "known" if plans else "unknown", "reason": None if plans else "legacy_scope_did_not_freeze_approval_baseline"}
    bundle = validate_content_update(snapshot)
    fields = [{"target": entry["target"], "public_fields": entry["public_fields"],
        "field_evidence": entry.get("field_evidence", {}), "capture": entry.get("capture"),
        "editorial_metadata": entry.get("editorial_metadata"),
        "original_comparisons": entry.get("original_comparisons", {}), "excluded_fields": entry.get("excluded_fields", [])} for entry in entries]
    display = snapshot.get("derived_display", {})
    comparison = (binding or {}).get("content_comparison_baseline")
    authority = {"source_evidence": "separate_from_human_authorization", "human_decisions": display.get("manual_date_overrides", {})}
    if overlay and overlay["manifest"].get("manual_authorization"):
        manifest = overlay["manifest"]
        authority["new_human_authorization"] = {"schema_version": "human-article-date-authorization.v1",
            "instruction_reference": manifest["authorization_reference"], "instruction": manifest["manual_authorization"],
            "executor": manifest["actor"], "independent_source_pass": "not_applicable"}
    date_record = {**display, "article_id": snapshot["entity_id"]}
    if binding and not overlay:
        date_record = projected_date_proofs(date_record)
    if not overlay:
        for entry in entries:
            if entry["public_fields"].get("publication_date") and not date_record.get("publication_date"):
                capture = entry["capture"]
                proof = entry["field_evidence"]["publication_date"]
                date_record.update(publication_date=entry["public_fields"]["publication_date"],
                    publication_date_evidence={"text": proof["excerpt"], "date_kind": proof["date_kind"],
                        "source_url": entry["target"]["source_url"], "final_url": capture["final_url"],
                        "capture_sha256": capture["sha256"]})
    if overlay:
        from .owner_dates import date_proposed_view
        proposed = date_proposed_view(snapshot)
        date_record = {**proposed.get("derived_display", {}), "article_id": snapshot["entity_id"]}
    tables = snapshot.get("tables", {})
    assessments = []
    from .information_checks import attempt_identity, verified_packet_from_attempt
    for target in snapshot.get("information_targets", []):
        prefix = "article" if target["check_kind"] == "articles" else "meeting"
        rows = [row for row in tables.get(prefix + "_check_attempts", [])
            if attempt_identity(row, prefix) == target["occurrence_id"] and row["source_url"] == target["source_url"]]
        row = max(rows, key=lambda item: (item["checked_at"], item["attempt_id"]), default=None)
        if row:
            packet = verified_packet_from_attempt(row)
            assessments.append({"attempt_id": row["attempt_id"], "packet_sha256": row["packet_sha256"],
                "source_revision_sha256": row["source_revision_sha256"], "verification_status": row["verification_status"],
                "schema_version": packet.get("schema_version"), "comparisons": packet.get("comparisons", packet.get("owner_observation", {}).get("original_comparisons", {})),
                "excluded_fields": packet.get("excluded_fields"), "exclusions_status": "recorded" if "excluded_fields" in packet else "unknown",
                "reader": packet.get("reader"), "verified_information": packet.get("verified_information")})
    origins = [{key: row.get(key) for key in ("acquisition_item_id", "batch_id", "discovery_kind", "discovery_ref", "raw_url", "content_version_id")}
        for row in tables.get("acquisition_items", [])]
    pdf_links = [{"pdf_article_id": row["article_id"], "core_article_id": row.get("core_article_id"),
        "content_binding": "stored_core_article_id" if row.get("core_article_id") == snapshot["entity_id"] and snapshot["entity_kind"] == "article" else "unavailable",
        "reason": None if row.get("core_article_id") == snapshot["entity_id"] and snapshot["entity_kind"] == "article" else "no_stored_canonical_article_link"}
        for row in tables.get("pdf_intake_articles", [])]
    pdf_links.extend(deepcopy(snapshot.get("unlinked_pdf_references", [])))
    projection = {"schema_version": CONTRACT_VERSION, "candidate_sha256": digest(original),
        "entity_kind": snapshot["entity_kind"], "entity_id": snapshot["entity_id"], "scope": scope,
        "source_bindings": snapshot.get("information_targets", []), "taxonomy_bindings": taxonomies,
        "native_taxonomy_bindings": [{key: row.get(key) for key in
            ("report_id", "article_id", "taxonomy_id", "taxonomy_raw_sha256", "bundle_sha256")} | next((
                {"definition_yaml": value["definition_yaml"]} for value in (binding or {}).get("native_taxonomies", [])
                if value["taxonomy_id"] == row.get("taxonomy_id") and value["sha256"] == row.get("taxonomy_raw_sha256")), {})
            for row in tables.get("article_semantics", [])],
        "source_assessments": assessments,
        "source_entries": {"acquisition": origins, "pdf_links": pdf_links,
            "pdf_occurrences": [{key: row.get(key) for key in ("occurrence_id", "article_id", "source_document_sha256", "page", "content_sha256", "raw_url")}
                for row in tables.get("pdf_intake_article_occurrences", [])]},
        "proposed": {"source_fields": fields, "derived_display": display,
            "content_update_reference": "derived_display.content_update" if bundle else None},
        "inherited": {"baseline": inheritance, "fields": "all_approved_fields_except_selected_dates" if overlay else "approved_editorial_metadata_dates_and_authorizations" if binding else "none_implied",
            "selected_date_fields": sorted({field for entry in entries for field in entry["public_fields"]}) if overlay else []},
        "content_comparison_baseline": comparison or {"status": "not_applicable" if not bundle else "unknown",
            "reason": "scope_does_not_assert_new_content" if not bundle else "legacy_content_package_has_no_frozen_published_reference"},
        "approval_inheritance_baseline": inheritance, "authority": authority,
        "date_and_sort_basis": content_date_projection(date_record, snapshot) if snapshot["entity_kind"] in {"article", "pdf_article"} else {"status": "not_applicable", "reason": "event_dates_are_not_article_sort_dates"},
        "field_states": {"content_update": "present" if bundle else "unknown" if snapshot["entity_kind"] == "article" else "unsupported",
            "content_update_reason": None if bundle else "no_complete_content_package" if snapshot["entity_kind"] == "article" else "no_bound_canonical_article_body",
            "taxonomy": "source_only_hold" if taxonomies and all(entry.get("editorial_metadata") is None for entry in entries) else "present" if taxonomies else "inherited_approved" if overlay else "not_reviewed"},
        "semantic_review": "required_for_new_source_or_content_scope; structure_is_not_semantic_pass"}
    return projection


def validate_review_contract(snapshot, receipt):
    """An old source-only receipt cannot acquire a new meaning by replacing its SHA."""
    if BINDING not in snapshot or "publication_date_only" in snapshot:
        return
    if receipt.get("schema_version", "").startswith("owner-agent-") and not inherited_content_scope(snapshot):
        raise ValueError("owner v1/v2 receipt cannot approve complete content update scope")
    conclusion = receipt.get("conclusion")
    if conclusion is not None and (conclusion.get("candidate_sha256") != digest(snapshot)
            or conclusion.get("enrichment_contract_sha256") != digest(review_projection(snapshot))):
        raise ValueError("new content review must cover the exact complete enrichment contract")


def reviewed_content_receipt(snapshot, reviews):
    """Resolve an exact full-scope PASS, or its verified category-only inheritance."""
    accepted = _content_review(snapshot, reviews)
    return accepted[0] if accepted else None


def _content_review(snapshot, reviews):
    from .identity import is_identity_scope, OVERLAY as IDENTITY, verdict as identity_verdict
    if is_identity_scope(snapshot):
        if not identity_verdict(snapshot, reviews)["eligible"]:
            return None
        reference = snapshot[IDENTITY]["manifest"]["plan"]["participants"][0]
        return _content_review(reference["snapshot"], reference["reviews"])
    from .owner_dates import OVERLAY, _base, date_verdict
    if OVERLAY in snapshot:
        try:
            if not date_verdict(snapshot, reviews)["eligible"]:
                return None
            base, overlay = _base(snapshot)
            return _content_review(base, overlay["manifest"]["plan"]["baseline_reviews"])
        except (ValueError, KeyError, TypeError):
            return None
    binding = snapshot.get(BINDING, {})
    if ("publication_date_only" in snapshot or snapshot.get("entity_kind") != "article"
            or binding.get("schema_version") != SCOPE_VERSION
            or binding.get("scope") not in {CONTENT_SCOPE, INITIAL_SCOPE}):
        return None
    if inherited_content_scope(snapshot):
        return _inherited_content_review(snapshot, reviews)
    candidate, contract_sha = digest(snapshot), digest(review_projection(snapshot))
    for review in reviews:
        receipt = json.loads(review["receipt_json"])
        if receipt.get("schema_version") == "registry-category-correction-review.v1":
            accepted = _category_content_review(snapshot, review, receipt)
            if accepted:
                return accepted
            continue
        conclusion, claim = receipt.get("conclusion", {}), receipt.get("claim", {})
        if (review.get("status") != "pass" or review.get("candidate_sha256") != candidate
                or review.get("review_id") != digest([candidate, "pass", receipt])
                or receipt.get("schema_version", "").startswith("owner-agent-")
                or receipt.get("final_check_error") or not receipt.get("inspection") or not receipt.get("events_sha256")
                or claim.get("session_source") != "cron" or not claim.get("reviewer") or not claim.get("session_id")
                or conclusion.get("status") != "pass" or not conclusion.get("reason")
                or conclusion.get("candidate_sha256") != candidate
                or conclusion.get("enrichment_contract_sha256") != contract_sha):
            continue
        result = {"candidate_sha256": candidate, "enrichment_contract_sha256": contract_sha,
            "scope": binding["scope"], "review_id": review["review_id"],
            "reviewer": {"id": claim["reviewer"], "task_id": claim["session_id"]}}
        if binding["scope"] == INITIAL_SCOPE:
            if not _initial_summary(snapshot):
                continue
            summary = snapshot["derived_display"]["summary"]
            result["initial_summary"] = {**_body(snapshot), "summary": summary,
                "summary_sha256": hashlib.sha256(summary.encode()).hexdigest()}
        return result, snapshot, review
    return None


def _inherited_content_review(snapshot, reviews):
    """Content PASS stays on its original hash; the current receipt approves only its own scope."""
    binding, candidate = snapshot[BINDING], digest(snapshot)
    baseline = binding["approval_inheritance_baseline"]
    reference = baseline["content_review"]
    for review in reviews:
        try:
            receipt = json.loads(review["receipt_json"])
            context = review.get("_approval_context", {})
            original = context[reference["candidate_sha256"]]
            parent = context[baseline["candidate_sha256"]]
            accepted = _content_review(original["snapshot"], original["reviews"])
            inherited = _content_review(parent["snapshot"], parent["reviews"])
            if (not accepted or not inherited or not _matches_content_reference(snapshot, original["snapshot"], reference)
                    or any(accepted[0][key] != reference[key] or inherited[0][key] != reference[key]
                        for key in ("candidate_sha256", "enrichment_contract_sha256", "scope", "review_id"))
                    or review.get("status") != "pass" or review.get("candidate_sha256") != candidate):
                continue
            operation = binding["operation_scope"]
            reviewer = None
            if receipt.get("schema_version", "").startswith("owner-agent-"):
                if not operation.startswith("owner_source_fields."):
                    continue
                from .owner_checks import validate_review, REVIEW_VERSION, EDITORIAL_REVIEW_VERSION
                expected_version = EDITORIAL_REVIEW_VERSION if operation.endswith(".v2") else REVIEW_VERSION
                if receipt.get("schema_version") != expected_version:
                    continue
                validate_review(snapshot, receipt)
                if review["review_id"] != digest([candidate, "pass", receipt]):
                    continue
                reviewer = receipt["actor"]
            elif operation == "approved-public-category-only":
                if (receipt.get("schema_version") != "registry-category-correction-review.v1"
                        or receipt.get("scope") != operation or review["review_id"] != digest([candidate, receipt])
                        or receipt.get("previous_candidate_sha256") != baseline["candidate_sha256"]
                        or receipt.get("category_delta", {}).get("after") != snapshot["derived_display"].get("categories")
                        or snapshot.get("information_targets") != parent["snapshot"].get("information_targets")
                        or snapshot["tables"].get("article_check_attempts") != parent["snapshot"]["tables"].get("article_check_attempts")):
                    continue
                reviewer = inherited[0].get("source_reviewer", inherited[0]["reviewer"])
            else:
                # Reuse the full native receipt validator without granting a new content approval.
                conclusion, claim = receipt.get("conclusion", {}), receipt.get("claim", {})
                if (review["review_id"] != digest([candidate, "pass", receipt]) or receipt.get("final_check_error")
                        or not receipt.get("inspection") or not receipt.get("events_sha256")
                        or claim.get("session_source") != "cron" or not claim.get("reviewer") or not claim.get("session_id")
                        or conclusion.get("status") != "pass" or not conclusion.get("reason")
                        or conclusion.get("candidate_sha256") != candidate
                        or conclusion.get("enrichment_contract_sha256") != digest(review_projection(snapshot))):
                    continue
                reviewer = {"id": claim["reviewer"], "task_id": claim["session_id"]}
            source_review = (inherited[0].get("source_review", {key: inherited[0][key]
                for key in ("candidate_sha256", "review_id", "scope", "reviewer")})
                if operation == "approved-public-category-only" else
                {"candidate_sha256": candidate, "review_id": review["review_id"], "scope": operation, "reviewer": reviewer})
            proof = {**accepted[0], "source_reviewer": reviewer, "source_review": source_review}
            if operation == "approved-public-category-only":
                proof["category_correction"] = {"review_id": review["review_id"], "candidate_sha256": candidate,
                    "taxonomy_id": receipt["taxonomy"]["taxonomy_id"], "taxonomy_sha256": receipt["taxonomy"]["sha256"]}
            return proof, accepted[1], accepted[2]
        except (ValueError, KeyError, TypeError):
            continue
    return None


def _content_origin_from_reference(snapshot, reference):
    binding = snapshot[BINDING]
    original_binding = reference["binding"]
    if (reference["scope"] != binding["scope"] or any(binding.get(key) != original_binding.get(key)
            for key in ("schema_version", "scope", "capability", "content_comparison_baseline"))
            or _body(snapshot) != {key: reference[key] for key in ("content_version_id", "body_sha256")}
            or hashlib.sha256(snapshot["derived_display"]["summary"].encode()).hexdigest() != reference["summary_sha256"]):
        raise ValueError("category-only content approval body, version or summary differs")
    original = deepcopy(snapshot)
    original[BINDING] = deepcopy(original_binding)
    for key in ("categories", "metadata_provenance"):
        original["derived_display"].pop(key, None)
        if key in reference["display"]:
            original["derived_display"][key] = deepcopy(reference["display"][key])
    if digest(original) != reference["candidate_sha256"] or digest(review_projection(original)) != reference["enrichment_contract_sha256"]:
        raise ValueError("category-only original candidate or contract differs")
    return original


def _category_content_review(snapshot, review, receipt):
    candidate = digest(snapshot)
    baseline = snapshot[BINDING]["approval_inheritance_baseline"]
    reference = baseline.get("content_review")
    if (not reference or review.get("status") != "pass" or review.get("candidate_sha256") != candidate
            or review.get("review_id") != digest([candidate, receipt])
            or receipt.get("scope") != "approved-public-category-only"
            or receipt.get("previous_candidate_sha256") != baseline["candidate_sha256"]
            or receipt.get("category_delta", {}).get("after") != snapshot["derived_display"].get("categories")):
        return None
    try:
        original = _content_origin_from_reference(snapshot, reference)
        inherited = receipt["inherited_content_review"]
        if inherited["review_id"] != reference["review_id"] or not json.loads(inherited["receipt_json"]).get("claim"):
            return None
        accepted = _content_review(original, [inherited])
        if not accepted or any(accepted[0][key] != reference[key] for key in ("candidate_sha256", "enrichment_contract_sha256", "scope", "review_id")):
            return None
        proof, origin, native_review = accepted
        proof = {**proof, "category_correction": {"review_id": review["review_id"], "candidate_sha256": candidate,
            "taxonomy_id": receipt["taxonomy"]["taxonomy_id"], "taxonomy_sha256": receipt["taxonomy"]["sha256"]}}
        return proof, origin, native_review
    except (KeyError, ValueError, TypeError):
        return None


def bind_category_content(connection, original, snapshot):
    """Retain the real full-content approval while the existing writer changes categories only."""
    if BINDING not in original:
        return None
    accepted = _content_review(original, _candidate_reviews(connection, digest(original)))
    if not accepted:
        raise ValueError("category correction requires its original exact content approval")
    proof, origin, native_review = accepted
    if "publication_date_only" in original or inherited_content_scope(original):
        if not bind_existing_content(connection, snapshot, operation_scope="approved-public-category-only"):
            raise ValueError("category correction requires unchanged approved content")
        return native_review
    reference = {key: proof[key] for key in ("candidate_sha256", "enrichment_contract_sha256", "scope", "review_id")}
    reference.update(**_body(origin), summary_sha256=hashlib.sha256(origin["derived_display"]["summary"].encode()).hexdigest(),
        binding=deepcopy(origin[BINDING]), display={key: deepcopy(origin["derived_display"][key])
            for key in ("categories", "metadata_provenance") if key in origin["derived_display"]})
    snapshot[BINDING]["approval_inheritance_baseline"] = {**_approval_baseline(connection, snapshot, digest(original)),
        "content_review": reference}
    _content_origin_from_reference(snapshot, reference)
    return native_review


def inspect_candidate(database, candidate_sha256):
    """Read-only CLI handoff; no staging, review, publication or activation."""
    from .publication import resolve_database, public_revision
    database = resolve_database(database)
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        snapshot = _candidate(connection, candidate_sha256)
        projection = review_projection(snapshot)
        row = connection.execute("SELECT latest_candidate_sha256,published_candidate_sha256,is_visible FROM registry_publication WHERE entity_kind=? AND entity_id=?",
            (snapshot["entity_kind"], snapshot["entity_id"])).fetchone()
        reviews = [{"review_id": row[0], "status": row[1], "receipt": json.loads(row[2])}
            for row in connection.execute("SELECT review_id,status,receipt_json FROM registry_reviews WHERE candidate_sha256=? ORDER BY review_id", (candidate_sha256,))]
        return {"contract": projection, "contract_sha256": digest(projection),
            "registry_database": str(database), "publication": dict(zip(("latest_candidate_sha256", "published_candidate_sha256", "is_visible"), row)) if row else None,
            "reviews": reviews, "public_revision": public_revision(database, connection=connection),
            "activation": {"status": "unknown", "reason": "consumer_generation_must_be_verified_by_downstream"}, "write_performed": False}


def validate_input(database, snapshot, scope):
    """Validate the existing candidate schema against a live baseline without writes."""
    from copy import deepcopy
    from .publication import resolve_database
    database = resolve_database(database)
    if (not isinstance(snapshot, dict) or snapshot.get("schema_version") != "registry-public-candidate.v1"
            or snapshot.get("entity_kind") not in {"article", "pdf_article", "meeting", "pdf_meeting"}
            or not isinstance(snapshot.get("entity_id"), str) or not snapshot["entity_id"]
            or not isinstance(snapshot.get("tables"), dict) or not isinstance(snapshot.get("derived_display", {}), dict)):
        raise ValueError("unsupported candidate input schema")
    candidate = deepcopy(snapshot)
    candidate[BINDING] = {"schema_version": SCOPE_VERSION, "scope": scope,
        **({"native_taxonomies": deepcopy(candidate[BINDING]["native_taxonomies"])}
            if isinstance(candidate.get(BINDING), dict) and "native_taxonomies" in candidate[BINDING] else {})}
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        bind_content_scope(connection, candidate)
        projection = review_projection(candidate)
    return {"status": "valid", "contract": projection, "contract_sha256": digest(projection),
        "write_performed": False, "publication": "not_performed", "approval": "not_performed"}
