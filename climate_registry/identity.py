"""Exact semantic corrections on the existing candidate/owner-review path.

Locators and raw observations are never rewritten. Only the frozen, independently
approved projection applies the mapping and the current-body event relations.
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import closing
from copy import deepcopy

from .acquisition_review import digest, timestamp

SCOPE = "article_identity_relations.v1"
OVERLAY = "identity_correction"
REVIEW = "owner-agent-identity-review.v1"
DISPOSITIONS = {"article", "article_linked_to_dates", "pure_event", "portal", "irrelevant"}

_ROW_KEYS = {"pdf_intake_articles": ("article_id",), "pdf_intake_article_occurrences": ("occurrence_id",),
    "pdf_intake_calendar_items": ("occurrence_id",), "pdf_article_date_observations": ("observation_id",),
    "article_check_attempts": ("attempt_id",), "meeting_check_attempts": ("attempt_id",),
    "article_versions": ("version_id",), "discoveries": ("discovery_id",),
    "report_appearances": ("report_id","article_id"), "pdf_intake_documents": ("document_sha256",),
    "pdf_intake_document_sources": ("document_sha256","source_path"), "article_check_runs": ("run_id",),
    "meeting_check_runs": ("run_id",), "reports": ("report_id",),
    "climate_event_sources": ("event_source_id",), "climate_event_versions": ("event_id","record_version"),
    "article_content_versions": ("content_version_id",), "meeting_runs": ("meeting_run_id",),
    "meeting_run_items": ("meeting_run_id","acquisition_item_id"), "acquisition_batches": ("batch_id",)}


def is_identity_scope(snapshot):
    if OVERLAY not in snapshot:
        return False
    try:
        base = snapshot[OVERLAY]["manifest"]["plan"]["participants"][0]["snapshot"]
        return digest({key: value for key, value in snapshot.items() if key != OVERLAY}) == digest(
            {key: value for key, value in base.items() if key != OVERLAY})
    except (KeyError, TypeError):
        return False


def _reference(connection, kind, identity, *, allow_pending=False):
    from .publication import snapshot_entity
    from .verification import review_context
    row = connection.execute("""SELECT p.latest_candidate_sha256,p.published_candidate_sha256,p.is_visible,c.snapshot_json
        FROM registry_publication p LEFT JOIN registry_candidates c ON c.candidate_sha256=COALESCE(p.published_candidate_sha256,p.latest_candidate_sha256)
        WHERE p.entity_kind=? AND p.entity_id=?""", (kind, identity)).fetchone()
    if not row or not row[1] and not allow_pending:
        raise ValueError("identity correction requires an approved baseline or an exact pending disposition target")
    base = json.loads(row[3])
    baseline = row[1] or row[0]
    if digest(base) != baseline:
        raise ValueError("identity baseline hash differs")
    reviews = review_context(connection).get(baseline, [])
    if not reviews:
        # Imported legacy approvals are retained, but cannot substitute for source facts.
        reviews = [dict(zip(("review_id", "candidate_sha256", "status", "receipt_json"), value))
            for value in connection.execute("SELECT review_id,candidate_sha256,status,receipt_json FROM registry_reviews WHERE candidate_sha256=? AND status='accepted_legacy'", (baseline,))]
    return {"entity_kind": kind, "entity_id": identity,
        "expected_state": dict(zip(("latest_candidate_sha256", "published_candidate_sha256", "is_visible"), row[:3])),
        "source_snapshot_sha256": digest(snapshot_entity(connection, kind, identity, resolve_identity=False)),
        "snapshot": base, "reviews": reviews}


def plan_identity_check(database, kind, identity, *, participants=()):
    """A read-only exact scope; participants name aliases and related Dates."""
    from .publication import resolve_database, require_publication_migration
    database = resolve_database(database)
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        require_publication_migration(connection)
        connection.execute("BEGIN")
        keys = [(kind, identity), *map(tuple, participants)]
        if len(set(keys)) != len(keys) or kind not in {"article", "pdf_article", "meeting", "pdf_meeting"}:
            raise ValueError("identity scope must name distinct existing entities")
        refs = [_reference(connection, *key, allow_pending=index == 0) for index, key in enumerate(keys)]
    return {"schema_version": SCOPE, "plan": {"registry_database": str(database), "scope": SCOPE,
        "entity_kind": kind, "entity_id": identity, "participants": refs},
        "actor": {"id": "", "task_id": ""}, "authorization_reference": "", "audit_reference": "",
        "checked_at": "", "disposition": "article" if "article" in kind else "pure_event",
        "canonical_url": None, "aliases": [], "alias_operations": [], "relations": [], "evidence": []}


def _body(snapshot):
    article = next((row for row in snapshot.get("tables", {}).get("articles", [])
        if row["article_id"] == snapshot["entity_id"]), {})
    version = article.get("current_content_version_id")
    return {"content_version_id": version, "body_sha256": next((hashlib.sha256(row["markdown_content"].encode()).hexdigest()
        for row in snapshot.get("tables", {}).get("article_content_versions", []) if row["content_version_id"] == version), None)}


def linked_article_body(snapshot):
    """Only the reviewed relation on this exact body fixes Article classification."""
    mapping = snapshot.get("identity_mapping", {})
    body = _body(snapshot)
    return (mapping.get("disposition") == "article_linked_to_dates" and mapping.get("body") == body
        and any(relation.get("article_body") == body for relation in mapping.get("relations", [])))


def _scope(reference):
    snapshot = reference["snapshot"]
    return {"entity_kind": reference["entity_kind"], "entity_id": reference["entity_id"],
        "selection": "entity", "candidate_sha256": digest(snapshot), "approval_review_ids": sorted(row["review_id"] for row in reference["reviews"]), **_body(snapshot),
        "occurrence_ids": sorted(row["occurrence_id"] for table in ("pdf_intake_article_occurrences", "pdf_intake_calendar_items")
            for row in snapshot.get("tables", {}).get(table, []))}


def _source_reference(refs, scope):
    for ref in refs:
        full = _scope(ref)
        if scope == full:
            return ref
        if (set(scope) == set(full) and scope.get("occurrence_ids")
                and len(scope["occurrence_ids"]) == len(set(scope["occurrence_ids"]))
                and set(scope["occurrence_ids"]) <= set(full["occurrence_ids"])
                and all(scope.get(key) == full[key] for key in full if key not in {"selection", "occurrence_ids", "content_version_id", "body_sha256"})
                and scope.get("selection") == "occurrences"
                and scope.get("content_version_id") is None and scope.get("body_sha256") is None):
            return ref
    return None


def _whole_alias(alias):
    source = alias["source"]
    return source["selection"] == "entity"


def _validate(manifest):
    from .owner_checks import _actor, _nonempty, _url
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCOPE or manifest.get("plan", {}).get("scope") != SCOPE:
        raise ValueError("identity correction requires its explicit supported scope")
    fields = {"schema_version", "plan", "actor", "authorization_reference", "audit_reference", "checked_at",
        "disposition", "canonical_url", "aliases", "relations", "evidence"}
    if set(manifest) not in (fields, fields | {"alias_operations"}):
        raise ValueError("identity scope cannot include source-fact, content or date edits")
    _actor(manifest.get("actor"))
    timestamp(manifest["checked_at"])
    if not all(_nonempty(manifest.get(key)) for key in ("authorization_reference", "audit_reference")):
        raise ValueError("identity correction needs actual authorization and audit references")
    if manifest.get("disposition") not in DISPOSITIONS:
        raise ValueError("identity correction needs an explicit semantic disposition")
    if manifest.get("canonical_url") is not None:
        _url(manifest["canonical_url"])
        from climate_monitor.dedupe import canonical_url
        if canonical_url(manifest["canonical_url"]) != manifest["canonical_url"]:
            raise ValueError("identity correction URL must be canonical")
    refs = manifest["plan"]["participants"]
    if not refs or (refs[0]["entity_kind"], refs[0]["entity_id"]) != (manifest["plan"]["entity_kind"], manifest["plan"]["entity_id"]):
        raise ValueError("identity correction target binding differs")
    pending = refs[0]["expected_state"]["published_candidate_sha256"] is None
    if pending and (manifest["disposition"] not in {"portal", "irrelevant", "pure_event"}
            or len(refs) != 1 or manifest["aliases"] or manifest.get("alias_operations") or manifest["relations"] or manifest["canonical_url"] is not None):
        raise ValueError("unapproved baseline supports only a disposition-only scope without Article facts, URL, alias or relation changes")
    for ref in refs:
        if (ref["snapshot"]["entity_kind"], ref["snapshot"]["entity_id"]) != (ref["entity_kind"], ref["entity_id"]) or digest(ref["snapshot"]) != (ref["expected_state"]["published_candidate_sha256"] or ref["expected_state"]["latest_candidate_sha256"]):
            raise ValueError("identity participant approved snapshot differs")
    scopes = {_scope(ref)["candidate_sha256"]: _scope(ref) for ref in refs}
    alias_keys = set()
    for alias in manifest["aliases"]:
        if (set(alias) != {"source", "same_object", "evidence_ids"} or alias["same_object"] is not True
                or _source_reference(refs[1:], alias["source"]) is None):
            raise ValueError("alias requires an exact independently reviewed same-object source scope")
        key = (alias["source"]["entity_kind"], alias["source"]["entity_id"], tuple(alias["source"]["occurrence_ids"]))
        if key in alias_keys or key[0] not in ({"article", "pdf_article"} if "article" in refs[0]["entity_kind"] else {"meeting", "pdf_meeting"}):
            raise ValueError("alias repeats or changes the semantic object type")
        if refs[0]["entity_kind"] == "pdf_article" and key[0] == "article" and _whole_alias(alias):
            raise ValueError("a whole core Article requires an Article canonical target to retain its body and old ID")
        alias_keys.add(key)
    operations = manifest.get("alias_operations", [])
    if not isinstance(operations, list) or any(not isinstance(operation, dict)
            or set(operation) != {"action", "alias_sha256", "evidence_ids"}
            or operation["action"] not in {"revoke", "replace"}
            or not isinstance(operation["alias_sha256"], str) or len(operation["alias_sha256"]) != 64
            for operation in operations):
        raise ValueError("alias operation requires an explicit revoke/replace action and exact alias digest")
    if len({operation["alias_sha256"] for operation in operations}) != len(operations):
        raise ValueError("alias operation repeats the same exact scope")
    body = _body(refs[0]["snapshot"])
    relations = manifest["relations"]
    for relation in relations:
        if set(relation) != {"article_body", "event", "evidence_ids"} or relation["article_body"] != body or not body["content_version_id"]:
            raise ValueError("Article/Dates relation requires the exact retained current body version")
        event = relation["event"]
        reference = next((ref for ref in refs if _scope(ref) == event), None)
        if reference is None or reference["entity_kind"] not in {"meeting", "pdf_meeting"}:
            raise ValueError("Article/Dates relation needs the exact approved event edition")
    if (manifest["disposition"] == "article_linked_to_dates" and not relations) or (relations and manifest["disposition"] not in {"article_linked_to_dates", "pure_event"}):
        raise ValueError("linked Article disposition and exact Dates relations differ")
    if relations and refs[0]["entity_kind"] != "article":
        raise ValueError("Article/Dates relations require a canonical retained article body")
    evidence = manifest["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("semantic correction requires retained source evidence")
    ids = set()
    for proof in evidence:
        if (not isinstance(proof, dict) or set(proof) != {"id", "source", "text", "excerpt", "reason"}
                or not all(_nonempty(proof.get(key)) for key in ("id", "text", "excerpt", "reason"))
                or proof["source"] not in scopes.values() or proof["excerpt"] not in proof["text"] or proof["id"] in ids):
            raise ValueError("semantic evidence needs exact source scope and literal excerpt")
        # Evidence bytes must actually be retained with the named frozen source.
        source = next(ref["snapshot"] for ref in refs if _scope(ref) == proof["source"])
        texts = [row["markdown_content"] for row in source.get("tables", {}).get("article_content_versions", [])]
        for table in ("article_check_attempts", "meeting_check_attempts"):
            texts.extend(row["packet_json"] for row in source.get("tables", {}).get(table, []))
        texts.extend(row["occurrence_json"] for row in source.get("tables", {}).get("pdf_intake_article_occurrences", []))
        texts.extend(row["item_json"] for row in source.get("tables", {}).get("pdf_intake_calendar_items", []))
        def captured_strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from captured_strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from captured_strings(item)
        retained = []
        for text in texts:
            retained.append(text)
            try:
                retained.extend(captured_strings(json.loads(text)))
            except (ValueError, TypeError):
                pass
        if not any(proof["text"] in text for text in retained):
            raise ValueError("semantic evidence is absent from retained source bytes")
        ids.add(proof["id"])
    for change in [*manifest["aliases"], *operations, *relations]:
        if not change["evidence_ids"] or set(change["evidence_ids"]) - ids:
            raise ValueError("every mapping and relation needs semantic evidence")
    return manifest


def _frozen(snapshot):
    overlay = snapshot[OVERLAY]
    manifest = _validate(overlay["manifest"])
    base = manifest["plan"]["participants"][0]["snapshot"]
    if digest({key: value for key, value in snapshot.items() if key != OVERLAY}) != digest({key: value for key, value in base.items() if key != OVERLAY}):
        raise ValueError("identity correction cannot modify source content, dates or summaries")
    from .owner_dates import _baseline_view
    reference = manifest["plan"]["participants"][0]
    if reference["expected_state"]["published_candidate_sha256"] is None:
        view = {"schema_version": base["schema_version"], "entity_kind": base["entity_kind"], "entity_id": base["entity_id"],
            "tables": {}, "dependencies": {}, "sources": [], "information_targets": [], "derived_display": {}, "supplements": []}
        inherited = {"eligible": False, "primary": None, "observations": {}, "reviewed_summary": None,
            "reasons": ["reviewed_non_article_disposition"], "disposition_only": True}
        return view, inherited, manifest
    view, verdict = _baseline_view(base, reference["reviews"])
    return view, verdict, manifest


def _current(connection, snapshot, *, allow_published=False):
    from .publication import snapshot_entity
    _, _, manifest = _frozen(snapshot)
    for index, ref in enumerate(manifest["plan"]["participants"]):
        state = connection.execute("SELECT latest_candidate_sha256,published_candidate_sha256,is_visible FROM registry_publication WHERE entity_kind=? AND entity_id=?", (ref["entity_kind"], ref["entity_id"])).fetchone()
        expected = ref["expected_state"]
        allowed_publication = {expected["published_candidate_sha256"]}
        if index == 0 and allow_published:
            allowed_publication.add(digest(snapshot))
        if (not state or state[1] not in allowed_publication or state[2] != expected["is_visible"]
                or state[0] != (digest(snapshot) if index == 0 else expected["latest_candidate_sha256"])
                or digest(snapshot_entity(connection, ref["entity_kind"], ref["entity_id"], resolve_identity=False)) != ref["source_snapshot_sha256"]):
            raise ValueError("identity correction source, body, visibility or approval scope is stale")


def ingest_identity_check(connection, database, manifest, apply):
    _validate(manifest)
    refs = manifest["plan"]["participants"]
    snapshot = {**deepcopy(refs[0]["snapshot"]), OVERLAY: {"manifest": deepcopy(manifest)}}
    sha = digest(snapshot)
    latest = connection.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", (snapshot["entity_kind"], snapshot["entity_id"])).fetchone()
    if latest and latest[0] == sha:
        _current(connection, snapshot, allow_published=True)
        return {"status": "no-op", "candidate_sha256": sha}
    current = [_reference(connection, ref["entity_kind"], ref["entity_id"], allow_pending=index == 0) for index, ref in enumerate(refs)]
    if current != refs:
        raise ValueError("identity correction plan is stale")
    _frozen(snapshot)
    public_view(snapshot, {OVERLAY: {"review_id": "pending"}})
    if not apply:
        return {"status": "dry_run", "candidate_sha256": digest(snapshot)}
    # Stage through the shared immutable candidate and publication pointer writer.
    from .publication import stage_snapshot
    sha = stage_snapshot(connection, snapshot, basis=SCOPE)
    return {"status": "staged", "candidate_sha256": sha, "review_schema_version": REVIEW}


def review_template(snapshot):
    _frozen(snapshot)
    return {"schema_version": REVIEW, "review_scope": SCOPE, "candidate_sha256": digest(snapshot),
        "identity_scope_sha256": digest(snapshot[OVERLAY]), "actor": {"id": "", "task_id": ""},
        "authorization_reference": "", "audit_reference": "", "reviewed_at": "", "decision": "", "reason": ""}


def validate_review(snapshot, receipt):
    from .owner_checks import _actor, _nonempty
    _, _, manifest = _frozen(snapshot)
    if (receipt.get("schema_version") != REVIEW or receipt.get("review_scope") != SCOPE or receipt.get("decision") != "pass"
            or receipt.get("candidate_sha256") != digest(snapshot) or receipt.get("identity_scope_sha256") != digest(snapshot[OVERLAY])):
        raise ValueError("identity correction requires a new exact-scope independent PASS")
    _actor(receipt["actor"])
    if (any(receipt["actor"][key] == manifest["actor"][key] for key in ("id", "task_id"))
            or timestamp(receipt["reviewed_at"]) < timestamp(manifest["checked_at"])
            or not all(_nonempty(receipt.get(key)) for key in ("authorization_reference", "audit_reference", "reason"))):
        raise ValueError("identity correction requires an actual independent reviewer and audit references")


def verdict(snapshot, reviews):
    try:
        _, inherited, _ = _frozen(snapshot)
        for review in reviews:
            receipt = json.loads(review["receipt_json"])
            if review.get("candidate_sha256") == digest(snapshot) and review.get("status") == "pass" and review.get("review_id") == digest([digest(snapshot), "pass", receipt]):
                validate_review(snapshot, receipt)
                return {**inherited, OVERLAY: {"review_id": review["review_id"], "receipt": receipt}}
    except (ValueError, KeyError, TypeError):
        pass
    return {"eligible": False, "primary": None, "observations": {}, "reviewed_summary": None,
        "reasons": ["identity_scope_or_independent_review_invalid"]}


def _revoke_aliases(view, manifest, candidate, review_id):
    """Subtract only explicitly reviewed ownership; immutable prior views stay intact."""
    mapping = view.get("identity_mapping", {})
    active = mapping.get("aliases", [])
    removed = []
    for operation in manifest.get("alias_operations", []):
        alias = next((alias for alias in active if digest(alias) == operation["alias_sha256"]), None)
        if alias is None:
            raise ValueError("alias operation digest does not match the current approved alias scope")
        source = alias["source"]
        ref = next((ref for ref in manifest["plan"]["participants"][1:]
            if (ref["entity_kind"], ref["entity_id"]) == (source["entity_kind"], source["entity_id"])), None)
        if ref is None or ref["expected_state"]["latest_candidate_sha256"] != ref["expected_state"]["published_candidate_sha256"]:
            raise ValueError("held: alias operation requires the exact current approved source participant scope")
        replacement = [item for item in manifest["aliases"] if (item["source"]["entity_kind"], item["source"]["entity_id"]) == (source["entity_kind"], source["entity_id"])]
        if bool(replacement) != (operation["action"] == "replace"):
            raise ValueError("alias replace requires a new exact source scope; revoke cannot re-add that source")
        from .owner_dates import _baseline_view
        source_view, _ = _baseline_view(ref["snapshot"], ref["reviews"])
        nested = source_view.get("identity_mapping", {}).get("aliases", []) if _whole_alias(alias) else []
        operation_digests = {item["alias_sha256"] for item in manifest.get("alias_operations", [])}
        if any(digest(item) not in operation_digests for item in nested):
            raise ValueError("held: revoking a group requires each inherited alias and its exact participant scope")
        active_authorizations = {decision["authorization_id"] for decision in view.get("derived_display", {}).get("manual_date_overrides", {}).values()}
        if any(item["authorization_id"] in active_authorizations
                and (item["source"]["entity_kind"], item["source"]["entity_id"]) == (source["entity_kind"], source["entity_id"])
                for item in mapping.get("date_authorization_mappings", [])):
            raise ValueError("held: transferred human date requires an explicit date authorization correction before alias revocation")
        if source_view.get("identity_mapping", {}).get("relations"):
            raise ValueError("held: related Article/Dates participants require an exact relation correction before alias revocation")
        removed.append((alias, operation))
    if not removed:
        return []
    removed_digests = {digest(alias) for alias, _ in removed}
    mapping["aliases"] = [alias for alias in active if digest(alias) not in removed_digests]
    mapping.setdefault("revoked_aliases", []).extend({"alias_sha256": digest(alias), "alias": deepcopy(alias),
        "action": operation["action"], "candidate_sha256": candidate, "review_id": review_id,
        "evidence_ids": deepcopy(operation["evidence_ids"])} for alias, operation in removed)
    tables = view.get("tables", {})
    source_articles = {alias["source"]["entity_id"] for alias, _ in removed if _whole_alias(alias) and alias["source"]["entity_kind"] == "article"}
    source_pdfs = {alias["source"]["entity_id"] for alias, _ in removed if _whole_alias(alias) and alias["source"]["entity_kind"] == "pdf_article"}
    source_meetings = {alias["source"]["entity_id"] for alias, _ in removed if _whole_alias(alias) and alias["source"]["entity_kind"] == "meeting"}
    if source_meetings:
        removed_sources = {row["event_source_id"] for row in tables.get("climate_event_sources", []) if row["event_id"] in source_meetings}
        for table in ("climate_event_sources", "climate_event_versions"):
            tables[table] = [row for row in tables.get(table, []) if row["event_id"] not in source_meetings]
        tables["meeting_check_attempts"] = [row for row in tables.get("meeting_check_attempts", []) if row.get("event_source_id") not in removed_sources]
    retained_occurrences = {identity for alias in mapping["aliases"] for identity in alias["source"]["occurrence_ids"]}
    occurrences = ({identity for alias, _ in removed for identity in alias["source"]["occurrence_ids"]}
        | {row["occurrence_id"] for row in tables.get("pdf_intake_article_occurrences", []) if row["article_id"] in source_pdfs}) - retained_occurrences
    for table in ("article_versions", "discoveries", "report_appearances"):
        tables[table] = [row for row in tables.get(table, []) if row.get("article_id") not in source_articles]
    for table in ("pdf_intake_article_occurrences", "pdf_intake_calendar_items", "article_check_attempts", "meeting_check_attempts"):
        tables[table] = [row for row in tables.get(table, []) if row.get("occurrence_id") not in occurrences]
    pdf_ids = {row["article_id"] for row in tables.get("pdf_intake_article_occurrences", [])}
    tables["pdf_intake_articles"] = [row for row in tables.get("pdf_intake_articles", []) if row["article_id"] in pdf_ids]
    tables["pdf_article_date_observations"] = [row for row in tables.get("pdf_article_date_observations", []) if row["article_id"] in pdf_ids]
    dependencies = view.get("dependencies", {})
    documents = {row["source_document_sha256"] for table in ("pdf_intake_article_occurrences", "pdf_intake_calendar_items") for row in tables.get(table, [])}
    reports = {row["report_id"] for row in tables.get("report_appearances", [])}
    for table in ("pdf_intake_documents", "pdf_intake_document_sources"):
        dependencies[table] = [row for row in dependencies.get(table, []) if row["document_sha256"] in documents]
    dependencies["reports"] = [row for row in dependencies.get("reports", []) if row["report_id"] in reports]
    if source_meetings:
        native_sources = tables.get("climate_event_sources", [])
        contents = {row["content_version_id"] for row in native_sources}
        native_runs = {row["meeting_run_id"] for row in native_sources + tables.get("climate_event_versions", [])}
        dependencies["article_content_versions"] = [row for row in dependencies.get("article_content_versions", []) if row["content_version_id"] in contents]
        dependencies["meeting_runs"] = [row for row in dependencies.get("meeting_runs", []) if row["meeting_run_id"] in native_runs]
        dependencies["meeting_run_items"] = [row for row in dependencies.get("meeting_run_items", []) if row["meeting_run_id"] in native_runs]
        batches = {row["batch_id"] for row in dependencies["meeting_runs"]}
        dependencies["acquisition_batches"] = [row for row in dependencies.get("acquisition_batches", []) if row["batch_id"] in batches]
    for prefix in ("article", "meeting"):
        run_ids = {row["run_id"] for row in tables.get(prefix + "_check_attempts", [])}
        dependencies[prefix + "_check_runs"] = [row for row in dependencies.get(prefix + "_check_runs", []) if row["run_id"] in run_ids]
    return removed


def public_view(snapshot, result):
    if OVERLAY not in result:
        raise ValueError("unapproved identity mapping cannot be projected")
    view, _, manifest = _frozen(snapshot)
    target = (snapshot["entity_kind"], snapshot["entity_id"])
    view["_approved_candidate_sha256"] = digest(snapshot)
    prior_mapping = deepcopy(view.get("identity_mapping"))
    _revoke_aliases(view, manifest, digest(snapshot), result[OVERLAY]["review_id"])
    retained_mapping = view.get("identity_mapping", {})
    own_locators = sorted(set((prior_mapping or {}).get("own_locators", []) +
        [row["canonical_url"] for row in view.get("tables", {}).get("articles" if target[0] == "article" else "pdf_intake_articles", [])
            if row["article_id"] == target[1]] + ([manifest["canonical_url"]] if manifest["canonical_url"] else [])))
    view["identity_mapping"] = {"schema_version": SCOPE, "canonical": {"entity_kind": target[0], "entity_id": target[1]},
        "approved_snapshot_sha256": digest(snapshot), "review_id": result[OVERLAY]["review_id"],
        "disposition": manifest["disposition"], "body": _body(snapshot),
        "aliases": deepcopy(retained_mapping.get("aliases", [])), "relations": deepcopy(manifest["relations"]),
        "revoked_aliases": deepcopy(retained_mapping.get("revoked_aliases", [])), "own_locators": own_locators,
        "source_references": [_scope(ref) for ref in manifest["plan"]["participants"]],
        "locators": sorted(set(own_locators + [locator for alias in retained_mapping.get("aliases", [])
            if _whole_alias(alias) for locator in alias.get("source_locators", [])])),
        "prior_mappings": [prior_mapping] if prior_mapping else []}
    if manifest["canonical_url"]:
        table = "articles" if target[0] == "article" else "pdf_intake_articles"
        for row in view["tables"].get(table, []):
            row["canonical_url"] = manifest["canonical_url"]
    for alias in manifest["aliases"]:
        ref = _source_reference(manifest["plan"]["participants"], alias["source"])
        # Preserve evidence and citations under original IDs; no second current body.
        view.setdefault("identity_history", []).append(deepcopy(ref))
        from .owner_dates import _baseline_view
        source_view, _ = _baseline_view(ref["snapshot"], ref["reviews"])
        tables = source_view.get("tables", {})
        projected_alias = deepcopy(alias)
        source_mapping = source_view.get("identity_mapping", {})
        projected_alias["source_locators"] = sorted(set(source_mapping.get("own_locators", []) +
            [row["canonical_url"] for table in ("articles", "pdf_intake_articles") for row in tables.get(table, [])
                if row["article_id"] == ref["entity_id"]]))
        view["identity_mapping"]["aliases"].append(projected_alias)
        if _whole_alias(alias):
            source_mapping = source_view.get("identity_mapping", {})
            view["identity_mapping"]["aliases"].extend(deepcopy(source_mapping.get("aliases", [])))
            view["identity_mapping"]["locators"] = sorted(set(view["identity_mapping"]["locators"] +
                source_mapping.get("locators", []) + projected_alias["source_locators"]))
        if _whole_alias(alias) and target[0] == "article" and ref["entity_kind"] == "article":
            for table in ("article_versions", "discoveries", "report_appearances"):
                keys = _ROW_KEYS[table]
                present = {tuple(row[key] for key in keys) for row in view["tables"].get(table, [])}
                view["tables"].setdefault(table, []).extend(deepcopy(row) for row in tables.get(table, [])
                    if tuple(row[key] for key in keys) not in present)
            reports = {row["report_id"] for row in view["dependencies"].get("reports", [])}
            view["dependencies"].setdefault("reports", []).extend(deepcopy(row) for row in source_view.get("dependencies", {}).get("reports", []) if row["report_id"] not in reports)
        if _whole_alias(alias) and target[0] == "meeting" and ref["entity_kind"] == "meeting":
            # Native source/version IDs remain lineage; only the target event row supplies facts.
            for section, names in (("tables", ("climate_event_sources", "climate_event_versions", "meeting_check_attempts")),
                    ("dependencies", ("article_content_versions", "meeting_runs", "meeting_run_items", "acquisition_batches", "meeting_check_runs"))):
                for table in names:
                    keys = _ROW_KEYS[table]
                    present = {tuple(row[key] for key in keys) for row in view.get(section, {}).get(table, [])}
                    view.setdefault(section, {}).setdefault(table, []).extend(deepcopy(row)
                        for row in source_view.get(section, {}).get(table, []) if tuple(row[key] for key in keys) not in present)
        for table in ("pdf_intake_documents", "pdf_intake_document_sources"):
            existing = {digest(row) for row in view["dependencies"].get(table, [])}
            view["dependencies"].setdefault(table, []).extend(deepcopy(row) for row in source_view.get("dependencies", {}).get(table, []) if digest(row) not in existing)
        for table in ("pdf_intake_articles", "pdf_intake_article_occurrences", "pdf_article_date_observations", "pdf_intake_calendar_items", "article_check_attempts", "meeting_check_attempts"):
            rows = deepcopy(tables.get(table, []))
            selected = ({row["occurrence_id"] for name in ("pdf_intake_article_occurrences", "pdf_intake_calendar_items")
                for row in tables.get(name, [])} if _whole_alias(alias) else set(alias["source"]["occurrence_ids"]))
            if table in {"pdf_intake_article_occurrences", "pdf_intake_calendar_items", "article_check_attempts", "meeting_check_attempts"}:
                rows = [row for row in rows if row.get("occurrence_id") in selected]
            elif table == "pdf_intake_articles":
                pdf_ids = {row["article_id"] for row in tables.get("pdf_intake_article_occurrences", []) if row["occurrence_id"] in selected}
                rows = [row for row in rows if row["article_id"] in pdf_ids]
            elif table == "pdf_article_date_observations" and not _whole_alias(alias):
                rows = []  # An item relation cannot transfer dates for an entire aggregate.
            if table == "pdf_intake_articles" and target[0] == "article":
                for row in rows:
                    row["core_article_id"] = target[1]
            if table.endswith("_check_attempts"):
                run_table = table.replace("_attempts", "_runs")
                runs = {row["run_id"] for row in rows}
                present = {row["run_id"] for row in view["dependencies"].get(run_table, [])}
                view["dependencies"].setdefault(run_table, []).extend(deepcopy(row) for row in source_view.get("dependencies", {}).get(run_table, []) if row["run_id"] in runs - present)
            keys = _ROW_KEYS[table]
            existing = {tuple(row[key] for key in keys) for row in view["tables"].get(table, [])}
            for row in rows:
                identity = tuple(row[key] for key in keys)
                if identity not in existing:
                    view["tables"].setdefault(table, []).append(row)
                    existing.add(identity)
    if view.get("unlinked_pdf_references"):
        linked_sources = {row["article_id"] for row in view.get("tables", {}).get("pdf_intake_articles", [])}
        view["unlinked_pdf_references"] = [row for row in view["unlinked_pdf_references"] if row["pdf_article_id"] not in linked_sources]
    display = view.setdefault("derived_display", {})
    display.pop("article_event_relations", None)
    if manifest["disposition"] in {"portal", "irrelevant", "pure_event"} and "article" in target[0]:
        for row in view["tables"].get("articles", []):
            row.update(publication_eligible=0, document_kind="landing_page" if manifest["disposition"] == "portal" else row["document_kind"])
        view["tables"]["pdf_intake_articles"] = []
    from .owner_dates import carry_manual_dates, _baseline_view
    inherited_decisions = deepcopy(display.get("manual_date_overrides", {}))
    date_mappings = deepcopy((prior_mapping or {}).get("date_authorization_mappings", []))
    for alias in manifest["aliases"]:
        ref = _source_reference(manifest["plan"]["participants"], alias["source"])
        source_view, _ = _baseline_view(ref["snapshot"], ref["reviews"])
        if not _whole_alias(alias):
            continue
        source_body, target_body = _body(ref["snapshot"]), _body(snapshot)
        if source_body["body_sha256"] and target_body["body_sha256"] and source_body["body_sha256"] != target_body["body_sha256"]:
            raise ValueError("conflicting approved bodies need a complete content correction candidate before identity merge")
        for field, decision in source_view.get("derived_display", {}).get("manual_date_overrides", {}).items():
            previous = inherited_decisions.get(field)
            if previous and previous["new_value"] != decision["new_value"]:
                raise ValueError("conflicting human dates require the necessary owner scope to be held")
            if previous:
                continue
            if decision["scope"] == "content_version" and source_body["body_sha256"] != target_body["body_sha256"]:
                raise ValueError("human content-version date cannot be applied to a different body")
            inherited_decisions[field] = deepcopy(decision)
            date_mappings.append({"authorization_id": decision["authorization_id"],
                "source": _scope(ref), "target_body": target_body})
    view["identity_mapping"]["date_authorization_mappings"] = date_mappings
    carry_manual_dates(view, inherited_decisions)
    if manifest["relations"]:
        from .owner_dates import _baseline_view
        events = []
        for relation in manifest["relations"]:
            ref = next(ref for ref in manifest["plan"]["participants"] if _scope(ref) == relation["event"])
            event_view, _ = _baseline_view(ref["snapshot"], ref["reviews"])
            native = event_view["tables"].get("climate_events", [])
            if native:
                events.append(deepcopy(native[0]))
            else:
                row = next(iter(event_view["tables"].get("pdf_intake_calendar_items", [])), None)
                if row is None:
                    raise ValueError("related approved event has no retained Dates facts")
                events.append({**json.loads(row["item_json"]), "event_id": ref["entity_id"]})
        display["article_event_relations"] = [{"article_body": relation["article_body"], "event": relation["event"],
            "facts": events[index] if index < len(events) else None} for index, relation in enumerate(manifest["relations"])]
        if manifest["disposition"] == "article_linked_to_dates":
            display["is_activity"] = False
    return view


def approved_aliases(snapshots):
    """Only permitted immutable public views can supply a reference mapping."""
    edges = {}
    for key, snapshot in snapshots.items():
        for alias in snapshot.get("identity_mapping", {}).get("aliases", []):
            if _whole_alias(alias):
                source = alias["source"]
                edges.setdefault((source["entity_kind"], source["entity_id"]), set()).add(key)
    aliases = {}
    def root(key, path):
        if key in path:
            raise ValueError("approved semantic mappings form a cycle")
        if key in aliases:
            return aliases[key]
        if key not in edges:
            return key
        targets = {root(target, path | {key}) for target in edges[key]}
        if len(targets) != 1:
            raise ValueError("approved semantic alias has multiple canonical targets")
        aliases[key] = targets.pop()
        return aliases[key]
    for key in edges:
        root(key, set())
    return aliases


def article_source_ids(snapshot):
    return {snapshot["entity_id"]} | {alias["source"]["entity_id"]
        for alias in snapshot.get("identity_mapping", {}).get("aliases", [])
        if _whole_alias(alias) and alias["source"]["entity_kind"] == "article"}


def selected_entities(snapshots, selected):
    selected = set(selected)
    aliases = approved_aliases(snapshots)
    selected |= {aliases[key] for key in selected if key in aliases}
    selected |= {key for key, snapshot in snapshots.items() for alias in snapshot.get("identity_mapping", {}).get("aliases", [])
        if (alias["source"]["entity_kind"], alias["source"]["entity_id"]) in selected}
    return selected


def reconcile_approved_views(snapshots):
    """Apply reviewed whole-entity and occurrence ownership to disposable views."""
    aliases = approved_aliases(snapshots)
    owners = {}
    for target, snapshot in snapshots.items():
        target = aliases.get(target, target)
        for alias in snapshot.get("identity_mapping", {}).get("aliases", []):
            for occurrence in alias["source"]["occurrence_ids"]:
                old = owners.setdefault(occurrence, target)
                if old != target:
                    raise ValueError("approved source occurrence has multiple semantic owners")
    for key, snapshot in list(snapshots.items()):
        if (key[0] in {"article", "pdf_article"} and snapshot.get("identity_mapping", {}).get("disposition") in {"portal", "irrelevant", "pure_event"}):
            del snapshots[key]
            continue
        if key in aliases:
            del snapshots[key]
            continue
        tables = snapshot.get("tables", {})
        for table in ("pdf_intake_article_occurrences", "pdf_intake_calendar_items"):
            if table not in tables:
                continue
            tables[table] = [row for row in tables[table] if owners.get(row["occurrence_id"], key) == key]
        if key[0] == "pdf_article":
            remaining = tables.get("pdf_intake_article_occurrences", [])
            primary = (snapshot.get("_verification", {}).get("primary") or {}).get("occurrence_id")
            if primary and primary not in {row["occurrence_id"] for row in remaining}:
                facts = next((json.loads(row["occurrence_json"]) for row in remaining
                    if row["occurrence_id"] in snapshot.get("_verification", {}).get("verified_occurrence_ids", [])), {})
                snapshot.setdefault("derived_display", {}).update(summary=facts.get("summary") or "", summary_provenance="verified_pdf_source")
                for row in tables.get("pdf_intake_articles", []):
                    row["title"] = facts.get("title") or facts.get("anchor_text") or "Verified article reference"
            ids = {row["article_id"] for row in remaining}
            tables["pdf_intake_articles"] = [row for row in tables.get("pdf_intake_articles", []) if row["article_id"] in ids]
            if not ids:
                del snapshots[key]
        elif key[0] == "pdf_meeting" and not tables.get("pdf_intake_calendar_items"):
            del snapshots[key]
    return snapshots


def retain_approved_identity(connection, snapshot):
    """A later content candidate keeps approved reference mappings as history."""
    row = connection.execute("""SELECT c.snapshot_json FROM registry_publication p JOIN registry_candidates c
        ON c.candidate_sha256=p.published_candidate_sha256 WHERE p.entity_kind=? AND p.entity_id=?""",
        (snapshot["entity_kind"], snapshot["entity_id"])).fetchone()
    if not row:
        if snapshot.get("approved_identity_history"):
            raise ValueError("identity history requires a retained approved Registry scope")
        return
    previous = json.loads(row[0])
    from .owner_dates import OVERLAY as DATE, _base
    while DATE in previous and not is_identity_scope(previous):
        previous, _ = _base(previous)
    expected = None
    if is_identity_scope(previous):
        from .verification import review_context
        expected = [{"snapshot": previous, "reviews": review_context(connection).get(digest(previous), [])}]
    elif previous.get("approved_identity_history"):
        expected = previous["approved_identity_history"]
    supplied = snapshot.get("approved_identity_history")
    if supplied and supplied != expected:
        raise ValueError("candidate identity history differs from its actual approved Registry scope")
    if expected:
        snapshot["approved_identity_history"] = deepcopy(expected)


def inherited_identity_views(snapshot):
    for reference in snapshot.get("approved_identity_history", []):
        previous = reference["snapshot"]
        result = verdict(previous, reference["reviews"])
        if OVERLAY not in result or (previous["entity_kind"], previous["entity_id"]) != (snapshot["entity_kind"], snapshot["entity_id"]):
            raise ValueError("inherited identity mapping lacks an exact same-entity approved scope")
        yield public_view(previous, result), result


def inherit_public_identity(snapshot, view):
    for original, result in inherited_identity_views(snapshot):
        mapping = deepcopy(original["identity_mapping"])
        if mapping["body"] != _body(snapshot):
            mapping.setdefault("historical_relations", []).extend(mapping.pop("relations", []))
            mapping["relations"] = []
            view.setdefault("derived_display", {}).pop("article_event_relations", None)
        else:
            if original.get("derived_display", {}).get("article_event_relations"):
                view.setdefault("derived_display", {})["article_event_relations"] = deepcopy(original["derived_display"]["article_event_relations"])
        for section, names in (() if result.get("disposition_only") else (("tables", ("pdf_intake_articles", "pdf_intake_article_occurrences", "pdf_intake_calendar_items", "article_check_attempts", "meeting_check_attempts", "article_versions", "discoveries", "report_appearances", "climate_event_sources", "climate_event_versions")),
                ("dependencies", ("pdf_intake_documents", "pdf_intake_document_sources", "article_check_runs", "meeting_check_runs", "reports", "article_content_versions", "meeting_runs", "meeting_run_items", "acquisition_batches")))):
            for table in names:
                if snapshot["entity_kind"] != "meeting" and table in {"climate_event_sources", "climate_event_versions", "article_content_versions", "meeting_runs", "meeting_run_items", "acquisition_batches"}:
                    continue
                keys = _ROW_KEYS[table]
                present = {tuple(row[key] for key in keys) for row in view.get(section, {}).get(table, [])}
                rows = original.get(section, {}).get(table, [])
                if table in {"article_versions", "discoveries", "report_appearances"}:
                    rows = [row for row in rows if row.get("article_id") != snapshot["entity_id"]]
                if table in {"climate_event_sources", "climate_event_versions"}:
                    rows = [row for row in rows if row.get("event_id") != snapshot["entity_id"]]
                view.setdefault(section, {}).setdefault(table, []).extend(deepcopy(row) for row in rows
                    if tuple(row[key] for key in keys) not in present)
        mapping.update(body=_body(snapshot), current_candidate_sha256=snapshot.get("_approved_candidate_sha256") or digest(snapshot))
        view["identity_mapping"] = mapping
        if snapshot["entity_kind"] == "article":
            parents = {alias["source"]["entity_id"] for alias in mapping["aliases"]
                if _whole_alias(alias) and alias["source"]["entity_kind"] == "pdf_article"}
            for row in view.get("tables", {}).get("pdf_intake_articles", []):
                if row["article_id"] in parents:
                    row["core_article_id"] = snapshot["entity_id"]
            view["tables"]["pdf_intake_articles"] = list({row["article_id"]: row for row in view.get("tables", {}).get("pdf_intake_articles", [])}.values())
        if linked_article_body(view):
            view.setdefault("derived_display", {})["is_activity"] = False
        view["identity_history"] = deepcopy(original.get("identity_history", []))
        table = "articles" if snapshot["entity_kind"] == "article" else "pdf_intake_articles"
        locator = next((row["canonical_url"] for row in original.get("tables", {}).get(table, [])
            if row["article_id"] == snapshot["entity_id"]), None)
        if locator:
            for row in view.get("tables", {}).get(table, []):
                if row["article_id"] == snapshot["entity_id"]:
                    row["canonical_url"] = locator
    return view


def validate_publication(connection, snapshot, receipt):
    from .publication import published_snapshots, ENTITY_TABLES
    sha = digest(snapshot)
    proof = {"candidate_sha256": sha, "status": "pass", "review_id": digest([sha, "pass", receipt]), "receipt_json": json.dumps(receipt)}
    result = verdict(snapshot, [proof])
    current = published_snapshots(connection, tuple(ENTITY_TABLES), include_hidden=True)
    key = snapshot["entity_kind"], snapshot["entity_id"]
    if result["eligible"]:
        current[key] = public_view(snapshot, result)
    else:
        current.pop(key, None)
    reconcile_approved_views(current)
