"""Item-scoped handoffs; Registry records and existing writers retain authority."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from climate_delivery.io import atomic_write_json, transaction_lock
from .acquisition_review import digest, now_stamp, timestamp

VERSION = "registry-item-lifecycle.v1"


def _load(root, database, *, require_existing=False):
    from .publication import resolve_database
    database = resolve_database(database)
    path = Path(root) / "item-lifecycle" / "state.json"
    if require_existing and not path.is_file():
        raise ValueError("owner approval requires existing readable lifecycle state")
    value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {
        "schema_version": VERSION, "registry_database": str(database),
        "items": {}, "disputes": {}, "decisions": {}}
    if not isinstance(value, dict) or value.get("schema_version") != VERSION:
        raise ValueError("unsupported item lifecycle state")
    if (not all(isinstance(value.get(key), dict) for key in ("items", "disputes", "decisions"))
            or any(not isinstance(item, dict) or not isinstance(item.get("candidates"), dict)
                or not all(item.get(key) for key in ("entity_kind", "entity_id")) for item in value["items"].values())
            or any(not isinstance(item, dict) or not isinstance(item.get("bindings"), dict)
                or not all(item.get(key) for key in ("candidate_sha256", "entity_kind", "entity_id", "text", "evidence", "options", "impact"))
                for item in value["disputes"].values())
            or any(not isinstance(item, dict) or not isinstance(item.get("reply"), dict)
                or not all(item["reply"].get(key) for key in ("decision_id", "kind", "actor", "reference", "received_at", "text", "target"))
                for item in value["decisions"].values())):
        raise ValueError("invalid item lifecycle state")
    resolve_database(database, frozen=value["registry_database"])
    return value


def _save(root, state):
    atomic_write_json(Path(root) / "item-lifecycle" / "state.json", state)


def _key(database, kind, identity):
    return digest([str(Path(database).resolve()), kind, identity])


def _snapshot(connection, sha):
    row = connection.execute("SELECT entity_kind,entity_id,snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()
    if row is None or digest(json.loads(row[2])) != sha:
        raise ValueError("candidate snapshot is missing or changed")
    snapshot = json.loads(row[2])
    if (snapshot["entity_kind"], snapshot["entity_id"]) != tuple(row[:2]):
        raise ValueError("candidate entity differs from its immutable snapshot")
    return snapshot


def _reference(snapshot):
    return {"entity_kind": snapshot["entity_kind"], "entity_id": snapshot["entity_id"],
        "information_targets": snapshot.get("information_targets", []),
        "enrichment_scope": snapshot.get("enrichment_scope"),
        "identity_scope_sha256": digest(snapshot["identity_correction"]) if "identity_correction" in snapshot else None,
        "date_scope": snapshot.get("publication_date_only", {}).get("manifest", {}).get("plan"),
        "content_versions": [{key: row.get(key) for key in ("article_id", "content_version_id", "content_sha256")}
            for row in snapshot.get("tables", {}).get("article_content_versions", [])]}


def record_items(root, database, candidate_sha256s):
    """Retain pending versions even when a date-only publication moves latest."""
    from .publication import resolve_database
    database = resolve_database(database)
    with transaction_lock(Path(root), "item-lifecycle"):
        state = _load(root, database)
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
            for sha in candidate_sha256s:
                snapshot = _snapshot(connection, sha)
                key = _key(database, snapshot["entity_kind"], snapshot["entity_id"])
                item = state["items"].setdefault(key, {"entity_kind": snapshot["entity_kind"],
                    "entity_id": snapshot["entity_id"], "candidates": {}})
                if sha not in item["candidates"]:
                    row = connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?",
                        (snapshot["entity_kind"], snapshot["entity_id"])).fetchone()
                    item["candidates"][sha] = {"bindings": _reference(snapshot),
                        "published_baseline": row[0] if row else None, "recorded_at": now_stamp()}
        _save(root, state)


def record_dispute(root, database, candidate_sha256, *, text, evidence, options, impact):
    """The same exact dispute has one identity; this records no notification."""
    if not all((text, evidence, options, impact)):
        raise ValueError("dispute needs actual text, evidence, options and impact")
    from .publication import resolve_database
    from .persistent import _exclusive_database_lock
    database = resolve_database(database)
    # Approval writers hold this same lock until their Registry commit.
    with _exclusive_database_lock(database):
        record_items(root, database, [candidate_sha256])
        return _record_dispute(root, database, candidate_sha256, text, evidence, options, impact)


def _record_dispute(root, database, candidate_sha256, text, evidence, options, impact):
    with transaction_lock(Path(root), "item-lifecycle"):
        state = _load(root, database)
        item = next(value for value in state["items"].values() if candidate_sha256 in value["candidates"])
        dispute = {"candidate_sha256": candidate_sha256, "entity_kind": item["entity_kind"],
            "entity_id": item["entity_id"], "bindings": item["candidates"][candidate_sha256]["bindings"],
            "text": text, "evidence": evidence, "options": options, "impact": impact}
        identity = digest(dispute)
        created = identity not in state["disputes"]
        if created:
            state["disputes"][identity] = {**dispute, "recorded_at": now_stamp()}
            _save(root, state)
        return {"dispute_id": identity, "created": created, "notification_status": "not_sent"}


def _article_dependency(connection, state, article, version):
    """Resolve a source body, including retained work displaced by a G1 overlay."""
    def contains(sha):
        snapshot = _snapshot(connection, sha)
        tables = snapshot.get("tables", {})
        return (bool(version) and snapshot["entity_kind"] == "article" and snapshot["entity_id"] == article
            and any(row.get("article_id") == article and row.get("current_content_version_id") == version for row in tables.get("articles", []))
            and any(row.get("article_id") == article and row.get("content_version_id") == version for row in tables.get("article_content_versions", [])))
    publication = connection.execute("SELECT latest_candidate_sha256,published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id=?", (article,)).fetchone()
    latest, published = publication if publication else (None, None)
    published_matches = published and contains(published)
    published_dispute = next((identity for identity, item in state["disputes"].items()
        if item["candidate_sha256"] == published
        and not any(reply["reply"].get("dispute_id") == identity for reply in state["decisions"].values())), None) if published_matches else None
    if published_matches and not published_dispute:
        return {"approved": published, "candidates": [], "latest": None}
    retained = [sha for item in state["items"].values() if (item["entity_kind"], item["entity_id"]) == ("article", article)
        for sha in item["candidates"]]
    disputed = [item["candidate_sha256"] for item in state["disputes"].values()
        if (item["entity_kind"], item["entity_id"]) == ("article", article)]
    candidates = [sha for sha in dict.fromkeys([latest, *disputed, *retained]) if sha and contains(sha)]
    disputes = {identity: item for identity, item in state["disputes"].items() if item["candidate_sha256"] in candidates}
    unresolved = any(not any(reply["reply"].get("dispute_id") == identity for reply in state["decisions"].values())
        for identity in disputes)
    # Only the committed publication's frozen approval ancestry proves a historical body.
    # A standalone PASS (including display rollback evidence) is not publication history.
    if not unresolved:
        from .owner_dates import OVERLAY, _baseline_view, _frozen_scope
        from .enrichment_contract import _candidate_reviews
        seen, ancestor = set(), published
        while ancestor and ancestor not in seen:
            seen.add(ancestor)
            snapshot = _snapshot(connection, ancestor)
            if (snapshot["entity_kind"], snapshot["entity_id"]) != ("article", article):
                raise ValueError("published Article approval ancestry identity differs")
            _baseline_view(snapshot, _candidate_reviews(connection, ancestor))
            if contains(ancestor):
                return {"approved": ancestor, "candidates": [], "latest": None}
            if OVERLAY in snapshot:
                ancestor = _frozen_scope(snapshot)[2]["baseline_candidate_sha256"]
            else:
                ancestor = snapshot.get("enrichment_scope", {}).get("approval_inheritance_baseline", {}).get("candidate_sha256")
    from .enrichment_contract import _body
    published_body = _body(_snapshot(connection, published)) if published else None
    substantive = published_body and any(_body(_snapshot(connection, sha))["body_sha256"] != published_body["body_sha256"] for sha in candidates)
    from .content_enrichment import no_change_snapshot
    from .publication import information_ready
    latest_snapshot = _snapshot(connection, latest) if latest in candidates else None
    no_change = (not disputes and latest_snapshot is not None and no_change_snapshot(connection, latest_snapshot)
        and information_ready(connection, latest_snapshot))
    return {"approved": None, "candidates": candidates, "latest": latest if latest in candidates else None,
        "requires_approval": bool(disputes or (substantive and not no_change)), "published_dispute": published_dispute,
        "held_published_sha256": published if published_dispute else None}


def partition_items(connection, root, candidate_sha256s, *, require_information=True, invalid_entities=()):
    """Classify complete candidates and real Article -> Dates dependencies."""
    from .publication import information_ready, _information_groups
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve()
    state = _load(root, database)
    from .acquisition_review import _negative_review_receipts
    source_failures = _negative_review_receipts(root, database)
    groups = _information_groups(connection) if require_information else None
    items, snapshots = {}, {}
    pending = list(dict.fromkeys(candidate_sha256s))
    selected, queued, article_dependencies = set(pending), set(pending), {}
    for sha in pending:
        result = {"candidate_sha256": sha, "classification": "invalid", "reason": None}
        items[sha] = result
        try:
            snapshot = _snapshot(connection, sha)
            snapshots[sha] = snapshot
            for source in snapshot.get("tables", {}).get("climate_event_sources", []):
                key = (source["article_id"], source["content_version_id"])
                if key not in article_dependencies:
                    article_dependencies[key] = _article_dependency(connection, state, *key)
                for dependency in article_dependencies[key]["candidates"]:
                    if dependency not in queued:
                        pending.append(dependency)
                        queued.add(dependency)
            kind, identity = snapshot["entity_kind"], snapshot["entity_id"]
            result.update(item_key=_key(database, kind, identity), entity_kind=kind, entity_id=identity,
                bindings=_reference(snapshot))
            result["checks"] = [{key: row.get(key) for key in ("attempt_id", "run_id", "checked_at", "access_status", "verification_status")}
                for table in ("article_check_attempts", "meeting_check_attempts") for row in snapshot.get("tables", {}).get(table, [])]
            row = connection.execute("SELECT latest_candidate_sha256,published_candidate_sha256,is_visible FROM registry_publication WHERE entity_kind=? AND entity_id=?",
                (kind, identity)).fetchone()
            if row is None:
                raise ValueError("candidate has no publication record")
            result.update(latest_candidate_sha256=row[0], published_candidate_sha256=row[1], is_visible=bool(row[2]))
            result["reviews"] = [dict(zip(("review_id", "status", "reviewed_at"), review)) for review in connection.execute(
                "SELECT review_id,status,reviewed_at FROM registry_reviews WHERE candidate_sha256=? ORDER BY rowid", (sha,))]
            result["acquisition_reviews"] = [review for review in source_failures.get(sha, [])
                if kind == "article" and review["article_id"] == identity]
            if row[1] == sha:
                result.update(classification="ready", stage="approved", next_owner="G5", reason=None)
                continue
            if row[0] != sha:
                raise ValueError("candidate_changed: reconcile retained candidate against current publication")
            from .enrichment_contract import validate_content_baselines
            from .identity import is_identity_scope
            if is_identity_scope(snapshot):
                from .identity import _current
                _current(connection, snapshot)
            else:
                validate_content_baselines(connection, snapshot, sha)
            latest_review = result["reviews"][-1] if result["reviews"] else None
            if latest_review and latest_review["status"] in {"needs_correction", "rejected"}:
                result.update(classification="held", stage="needs_correction", next_owner="author",
                    reason="exact_candidate_review_" + latest_review["status"], review_id=latest_review["review_id"])
                continue
            if result["acquisition_reviews"]:
                latest = result["acquisition_reviews"][-1]
                result.update(classification="held", stage="needs_correction", next_owner="author",
                    reason="exact_acquisition_review_" + latest["status"], receipt_sha256=latest["receipt_sha256"])
                continue
            result.update(classification="ready", stage="awaiting_independent_review", next_owner="G10")
            from .content_enrichment import no_change_snapshot
            if not any((item["entity_kind"], item["entity_id"]) == (kind, identity) for item in state["disputes"].values()) and no_change_snapshot(connection, snapshot):
                result.update(stage="no_change", next_owner=None, reason="source_only_no_substantive_change")
            disputes = [(dispute_id, dispute) for dispute_id, dispute in state["disputes"].items()
                if (dispute["entity_kind"], dispute["entity_id"]) == (kind, identity)]
            current_disputes = [(dispute_id, dispute) for dispute_id, dispute in disputes
                if dispute["candidate_sha256"] == sha and dispute["bindings"] == _reference(snapshot)]
            # A current exact dispute/reply rechecks applicability without rebinding an old reply.
            for dispute_id, dispute in current_disputes or disputes:
                replies = [value for value in state["decisions"].values() if value["reply"].get("dispute_id") == dispute_id]
                applicable = dispute["candidate_sha256"] == sha and dispute["bindings"] == _reference(snapshot)
                if not applicable or not replies:
                    result.update(classification="held", stage="owner_choice", next_owner="owner",
                        reason="dispute_inputs_changed" if not applicable else "awaiting_actual_reply", dispute_id=dispute_id)
                    break
                result.setdefault("business_decisions", []).extend(value["reply"]["decision_id"] for value in replies)
            if result["classification"] == "held":
                continue
            if snapshot.get("publication_date_only"):
                result.update(classification="held", stage="awaiting_independent_review", next_owner="G1",
                    reason="date_scope_uses_existing_owner_date_writer")
            elif is_identity_scope(snapshot):
                pass  # The exact inherited approved scope is checked above.
            elif require_information and not information_ready(connection, snapshot, information_groups=groups):
                result.update(classification="held", stage="awaiting_enrichment", next_owner="T1/G7",
                    reason="source_bound_information_not_ready")
        except (ValueError, KeyError, TypeError) as exc:
            result.update(classification="invalid", stage="requires_reconciliation", next_owner="author", reason=str(exc))
    # Only actual version-bearing event source edges propagate a hold. A shared PDF/run does not.
    invalid_by_entity = {(value["entity_kind"], value["entity_id"]): value for value in invalid_entities}
    for sha, snapshot in snapshots.items():
        if items[sha]["classification"] != "ready" or items[sha].get("stage") == "approved":
            continue
        for source in snapshot.get("tables", {}).get("climate_event_sources", []):
            scope = article_dependencies[(source["article_id"], source["content_version_id"])]
            if scope["approved"]:
                continue  # Only a real approval of this exact body covers its source relationship.
            dependency = scope.get("held_published_sha256") or scope["latest"] or next(iter(scope["candidates"]), None)
            blocked = invalid_by_entity.get(("article", source["article_id"])) or items.get(dependency)
            if scope.get("published_dispute"):
                blocked = {"classification": "held", "next_owner": "owner"}
            if blocked and blocked["classification"] == "ready" and not scope["requires_approval"]:
                continue
            if blocked and blocked.get("stage") == "awaiting_enrichment" and not scope["requires_approval"]:
                continue  # Dates has its own exact source check; Article T1 is not its approval gate.
            items[sha].update(classification="held", stage="dependency_held", next_owner=(blocked or {}).get("next_owner", "author"),
                reason="required_article_content_version_not_ready", dependency={"candidate_sha256": dependency,
                    "content_version_id": source["content_version_id"], "event_source_id": source["event_source_id"]})
            if scope.get("published_dispute"):
                items[sha]["dependency"]["dispute_id"] = scope["published_dispute"]
    return {name: [value for sha, value in items.items() if sha in selected and value["classification"] == name]
        for name in ("ready", "held", "invalid")}


def _require_correction_independence(connection, root, packet, session_id):
    if not session_id:
        return
    snapshots = {item.get("registry_candidate_sha256") or item["candidate_sha256"]:
        _snapshot(connection, item.get("registry_candidate_sha256") or item["candidate_sha256"]) for item in packet.get("candidates", [])}
    bound = set()
    for sha, snapshot in snapshots.items():
        tables = snapshot.get("tables", {})
        current = next((row.get("current_content_version_id") for row in tables.get("articles", [])
            if row["article_id"] == snapshot["entity_id"]), None)
        for row in tables.get("knowledge_versions", []):
            evidence = json.loads(row["evidence_json"])
            correction = evidence.get("correction_context") or {}
            if (correction.get("display")
                    and current in {None, correction.get("content_version_id")}
                    and all(snapshot.get("derived_display", {}).get(key) == value for key, value in correction["display"].items())
                    and any(source["acquisition_item_id"] == correction.get("item_id")
                        and source["content_version_id"] == correction.get("content_version_id") for source in tables.get("acquisition_items", []))
                    and any(body["content_version_id"] == correction.get("content_version_id")
                        and body["content_sha256"] == evidence.get("content_sha256") for body in tables.get("article_content_versions", []))):
                if not all(correction.get(key) for key in ("session_id", "execution_id", "hermes_database")):
                    raise ValueError("correction actual native author identity is missing; reconcile with a distinct correction")
                bound.add(sha)
                if correction["session_id"] == session_id:
                    raise ValueError("candidate correction requires another independent cron context")
    # Historical parent_context may identify the old reviewer, not the actual author.
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve()
    for path in Path(root).glob("*/acquisition-review/state.json"):
        state = json.loads(path.read_text())
        source = state["packet"]
        if Path(source["registry_database"]).resolve() != database:
            continue
        for item in source["candidates"] + state.get("excluded_candidates", []):
            sha = item.get("registry_candidate_sha256")
            if sha not in snapshots or sha in bound or not item.get("display") or item.get("revision", 1) < 2:
                continue
            snapshot = snapshots[sha]
            current = next((row.get("current_content_version_id") for row in snapshot["tables"].get("articles", [])
                if row["article_id"] == snapshot["entity_id"]), None)
            if current not in {None, item["identity"].get("content_version_id")}:
                continue
            archived = path.parent / "packets" / (state["packet_sha256"] + ".json")
            if (digest(source) != state["packet_sha256"] or json.loads(archived.read_text()) != source
                    or item["candidate_sha256"] != digest([item["identity"], sha])
                    or item["identity"]["article_id"] != snapshot["entity_id"]):
                raise ValueError("correction context binding changed")
            if all(snapshot.get("derived_display", {}).get(key) == value for key, value in item["display"].items()):
                raise ValueError("correction actual native author identity is missing; reconcile with a distinct correction")


def require_item_readiness(connection, root, packet, *, session_id=None):
    _require_correction_independence(connection, root, packet, session_id)
    shas = [item.get("registry_candidate_sha256") or item["candidate_sha256"] for item in packet.get("candidates", [])]
    partition = partition_items(connection, root, shas)
    if partition["held"] or partition["invalid"]:
        raise ValueError("selected complete candidate scope changed or is held; prepare a new ready subset")


def require_no_disputes(connection, root, packet, *, session_id=None):
    """Concurrent source drift still gets the existing exact-review disposition."""
    _require_correction_independence(connection, root, packet, session_id)
    shas = [item.get("registry_candidate_sha256") or item["candidate_sha256"] for item in packet.get("candidates", [])]
    partition = partition_items(connection, root, shas, require_information=False)
    if any(item.get("stage") in {"owner_choice", "dependency_held", "needs_correction"} for item in partition["held"]):
        raise ValueError("selected scope is held by an unresolved business dispute or exact review requiring correction")


def _delivery(database, revision, queue_dir, runtime_wiki_dir):
    result = {"target_public_revision": revision, "stage": "pending_sync", "next_owner": "G5",
        "verification": {"status": "unknown", "next_owner": "G6", "reason": "no supported consumer verification receipt"}}
    if queue_dir is not None:
        path = Path(queue_dir) / "public-refresh.json"
        if path.is_file():
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if receipt.get("public_registry_revision") == revision:
                result["writer_receipt"] = {"path": str(path.resolve()), "sha256": digest(receipt), "result": receipt}
                result["retry_after"] = receipt.get("retry_after")
    if runtime_wiki_dir is not None:
        from .pdf_pipeline import load_active_projection, _validated_projection, load_projection_manifest, approved_projection_snapshot
        try:
            prepared = result.get("writer_receipt", {}).get("result", {}).get("prepared_generation")
            if prepared and prepared.get("public_registry_revision") == revision:
                generation, metadata = _validated_projection(Path(runtime_wiki_dir).resolve(), prepared)
                if load_projection_manifest(generation, metadata) and approved_projection_snapshot(Path(runtime_wiki_dir), metadata, database=database):
                    result.update(stage="prepared", prepared_generation=metadata)
            generation, active = load_active_projection(Path(runtime_wiki_dir), Path(queue_dir) / "active.json" if queue_dir is not None else None)
            if active and active.get("public_registry_revision") == revision and load_projection_manifest(generation, active) and approved_projection_snapshot(Path(runtime_wiki_dir), active, database=database):
                result.update(stage="active", active_generation=active, next_owner="G6")
        except (RuntimeError, OSError, ValueError) as exc:
            result["generation_error"] = str(exc)
    return result


def item_status(root, database, *, candidate_sha256s=None, queue_dir=None, runtime_wiki_dir=None):
    """Read-only reconciliation; a saved work label never establishes approval."""
    from .publication import resolve_database, public_revision
    database = resolve_database(database)
    state = _load(root, database)
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        shas = list(candidate_sha256s) if candidate_sha256s is not None else list(dict.fromkeys([
            row[0] for row in connection.execute("SELECT latest_candidate_sha256 FROM registry_publication") if row[0]] +
            [sha for item in state["items"].values() for sha in item["candidates"]]))
        partition = partition_items(connection, root, shas)
        revision = public_revision(database, connection=connection)
        delivery = _delivery(database, revision, queue_dir, runtime_wiki_dir)
    for item in partition["ready"]:
        if item.get("stage") == "approved":
            item["delivery"] = delivery
    return {"schema_version": VERSION, "registry_database": str(database), "public_revision": revision,
        **partition, "decisions": state["decisions"], "disputes": state["disputes"]}


def export_items(root, database, **selection):
    result = item_status(root, database, **selection)
    return {**result, "handoff_sha256": digest(result), "notification_status": "not_sent"}


def import_decisions(root, database, replies):
    """Record actual replies only. Ordinary choices never confer review authority."""
    if not isinstance(replies, list) or not replies:
        raise ValueError("actual decision replies are required")
    with transaction_lock(Path(root), "item-lifecycle"):
        state = _load(root, database)
        results = []
        for reply in replies:
            if (not isinstance(reply, dict) or not isinstance(reply.get("target"), dict)
                    or not all(isinstance(reply.get(key), str) and reply[key].strip() for key in
                        ("decision_id", "kind", "actor", "reference", "received_at", "text"))):
                raise ValueError("reply needs id, kind, actual actor/reference/time/text and target")
            timestamp(reply["received_at"])
            identity = reply["decision_id"]
            if identity in state["decisions"]:
                if state["decisions"][identity]["reply"] != reply:
                    raise ValueError("decision id was reused with a different reply")
                results.append({"decision_id": identity, "status": "no-op"})
                continue
            if reply["kind"] == "business":
                dispute = state["disputes"].get(reply.get("dispute_id"))
                if not dispute or reply.get("candidate_sha256") != dispute["candidate_sha256"] or reply["target"] != {
                        "entity_kind": dispute["entity_kind"], "entity_id": dispute["entity_id"]} or not isinstance(reply.get("choice"), str) or not reply["choice"].strip():
                    raise ValueError("business reply must bind the exact dispute, candidate, target and choice")
            elif reply["kind"] == "human_date":
                from .owner_dates import validate_date_manifest
                manifest = reply.get("manifest") or {}
                validate_date_manifest(manifest)
                authorization = manifest.get("manual_authorization") or {}
                plan = manifest["plan"]
                if (authorization.get("instruction") != reply["text"] or authorization.get("authorized_by") != reply["actor"]
                        or manifest.get("authorization_reference") != reply["reference"]
                        or reply["target"] != {"entity_kind": plan["entity_kind"], "entity_id": plan["entity_id"]}
                        or Path(plan["registry_database"]).resolve() != Path(database).resolve()):
                    raise ValueError("explicit date reply differs from its exact G1 authorization")
            else:
                raise ValueError("unsupported human decision kind")
            state["decisions"][identity] = {"reply": deepcopy(reply), "recorded_at": now_stamp()}
            results.append({"decision_id": identity, "status": "recorded"})
        _save(root, state)
        return {"decisions": results}


def _manual_date_events(connection, sha, kind, identity, fields):
    """Retain revocations in the committed publication's exact approval ancestry."""
    from .owner_dates import OVERLAY, _baseline_view, _frozen_scope, _date_fields
    from .enrichment_contract import _candidate_reviews
    events, seen = {}, set()
    while sha:
        if sha in seen:
            raise ValueError("cyclic date authority approval ancestry")
        seen.add(sha)
        snapshot = _snapshot(connection, sha)
        if (snapshot["entity_kind"], snapshot["entity_id"]) != (kind, identity):
            raise ValueError("date authority approval ancestry belongs to another entity")
        _baseline_view(snapshot, _candidate_reviews(connection, sha))
        if OVERLAY in snapshot:
            overlay = _frozen_scope(snapshot)[2]
            manifest = overlay["manifest"]
            if manifest.get("manual_authorization"):
                for field in set(fields) & set(_date_fields(manifest["plan"])):
                    events.setdefault(field, digest([manifest, field]))
            sha = overlay["baseline_candidate_sha256"]
        else:
            sha = snapshot.get("enrichment_scope", {}).get("approval_inheritance_baseline", {}).get("candidate_sha256")
    return events


def resume_items(root, database, *, decision_id=None, queue_dir=None, backup_dir=None, **selection):
    """Only an explicit imported G1 date instruction has an executable action here."""
    state = _load(root, database)
    if decision_id is None:
        return {"status": "handoff", "items": item_status(root, database, queue_dir=queue_dir, **selection),
            "execution": "not_dispatched"}
    decision = state["decisions"].get(decision_id)
    if decision is None:
        raise ValueError("resume needs an actual imported decision")
    reply = decision["reply"]
    if reply["kind"] != "human_date":
        return {"status": "handoff", "decision_id": decision_id, "next_owner": "author/G10",
            "execution": "not_dispatched", "reason": "business choice is not factual or review authority"}
    if decision.get("result"):
        return {"status": "no-op", "decision_id": decision_id, "result": decision["result"]}
    if queue_dir is None or backup_dir is None:
        return {"status": "blocked", "decision_id": decision_id, "next_owner": "G1",
            "reason": "existing owner-date writer requires queue and backup directories"}
    from .publication import resolve_database
    from .owner_checks import ingest_owner_check
    original = reply["manifest"]
    manifests = decision.get("execution_manifests", [original])
    # Recover a committed authorization even if the external result write was lost.
    database = resolve_database(database)
    recovered = None
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        for sha, encoded in connection.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates WHERE entity_kind=? AND entity_id=?", tuple(reply["target"][key] for key in ("entity_kind", "entity_id"))):
            snapshot = json.loads(encoded)
            manifest = snapshot.get("publication_date_only", {}).get("manifest")
            if manifest in manifests:
                for review_id, receipt in connection.execute("SELECT review_id,receipt_json FROM registry_reviews WHERE candidate_sha256=? AND status='pass'", (sha,)):
                    proof = json.loads(receipt)
                    if proof.get("authorization") == manifest["manual_authorization"] and proof.get("candidate_sha256") == sha:
                        recovered = {"status": "approved", "candidate_sha256": sha, "review_id": review_id, "recovered": True}
                        break
    if recovered is None:
        from .owner_checks import plan_owner_check
        from .owner_dates import active_manual_dates, _baseline_view, DATE_FIELDS_SCOPE, ACTIVITY_SCOPE
        plan = original["plan"]
        planned = plan_owner_check(database, plan["entity_kind"], plan["entity_id"],
            date_fields=plan.get("date_fields") if plan["scope"] == DATE_FIELDS_SCOPE else None,
            activity_dates_only=plan["scope"] == ACTIVITY_SCOPE, human_date_override=True)
        # Re-evaluate human scope separately from an exact factual review binding.
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
            base = _snapshot(connection, plan["baseline_candidate_sha256"])
            original_view, _ = _baseline_view(base, plan["baseline_reviews"])
            previous = original_view.get("derived_display", {}).get("manual_date_overrides", {})
            current = active_manual_dates(connection, plan["entity_kind"], plan["entity_id"])
            fields = plan.get("date_fields") or ["activity_dates"]
            before_events = _manual_date_events(connection, plan["baseline_candidate_sha256"], plan["entity_kind"], plan["entity_id"], fields)
            current_events = _manual_date_events(connection, planned["plan"]["baseline_candidate_sha256"], plan["entity_kind"], plan["entity_id"], fields)
            if (current_events != before_events or any(current.get(field, {}).get("authorization_id") != previous.get(field, {}).get("authorization_id") for field in fields)):
                return {"status": "held", "decision_id": decision_id, "next_owner": "owner",
                    "reason": "later human date authority conflicts with the imported instruction"}
        old_target = original["observations"][0]["target"]
        if any(planned["plan"][key] != plan[key] for key in ("entity_kind", "entity_id")):
            return {"status": "held", "decision_id": decision_id, "next_owner": "owner",
                "reason": "instruction target identity changed"}
        targets = planned["plan"]["targets"]
        selected_target = next((target for target in targets if target["occurrence_id"] == old_target["occurrence_id"]), targets[0])
        manifest = {**deepcopy(original), "plan": planned["plan"]}
        manifest["observations"][0]["target"] = selected_target
        from .owner_dates import validate_date_manifest
        validate_date_manifest(manifest)
        record_items(root, database, [manifest["plan"]["expected_state"]["latest_candidate_sha256"]])
        with transaction_lock(Path(root), "item-lifecycle"):
            state = _load(root, database)
            attempts = state["decisions"][decision_id].setdefault("execution_manifests", [])
            if manifest not in attempts:
                attempts.append(manifest)
                _save(root, state)
        result = ingest_owner_check(database, manifest, queue_dir=queue_dir, backup_dir=backup_dir, apply=True)
    else:
        result = recovered
    with transaction_lock(Path(root), "item-lifecycle"):
        state = _load(root, database)
        state["decisions"][decision_id]["result"] = result
        _save(root, state)
    return {"status": "executed" if recovered is None else "recovered", "decision_id": decision_id,
        "result": result, "next_owner": "G5", "downstream": "not_dispatched"}
