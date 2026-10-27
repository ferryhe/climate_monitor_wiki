"""Frozen pre-G3 date scopes retain their byte contract, never their own new approval."""
from copy import deepcopy
import json
import sqlite3

import pytest

from climate_registry.acquisition_review import digest
from climate_registry import owner_dates as dates
from climate_registry.verification import evaluate_snapshot, permitted_view
from test_owner_publication_dates import _seed, _date_manifest, _date_review, _approve
from test_owner_checks import _manifest, _ingest, _review, approve_owner_check
from test_verified_publication import _reader


@pytest.fixture(autouse=True)
def ordered_native_checks(monkeypatch):
    import climate_registry.information_checks as checks
    monkeypatch.setattr(checks, "_now", lambda: "2026-10-01T00:00:00Z")


def _historical_projection(base, reviews):
    """The pre-G3 wire fixture: native-first and date proof after review identity."""
    verdict = evaluate_snapshot(base, reviews=reviews)
    verified = [row for row in verdict["observations"].values() if row["verified"]]
    native = [row for row in verified if row["entity_kind"] in {"article", "meeting"}]
    verdict["primary"] = (native or verified)[0]
    verdict["article_owner_primary"] = None
    for row in verified:
        owner = row.get("owner_agent_verification")
        if owner and "publication_date_evidence" in owner:
            proof = owner.pop("publication_date_evidence")
            owner["publication_date_evidence"] = proof
    return permitted_view(base, verdict)


def _history(tmp_path, source, *, later_owner=False):
    database = _seed(tmp_path, source="linked" if source == "native" else "pdf")
    kind, identity = ("article", "good") if source == "native" else ("pdf_article", "pdf-good")
    if later_owner:
        with sqlite3.connect(database) as db:
            cursor = db.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='occurrence'")
            row = dict(zip((column[0] for column in cursor.description), cursor.fetchone()))
            row.update(occurrence_id="occurrence-new", page=2)
            occurrence = json.loads(row["occurrence_json"])
            occurrence.update(occurrence_id="occurrence-new", page=2)
            row["occurrence_json"] = json.dumps(occurrence)
            db.execute("INSERT INTO pdf_intake_article_occurrences (" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
    manifest = _manifest(database, kind=kind, identity=identity, occurrence="occurrence-new" if later_owner else "occurrence",
        fields={"title": "Climate risk findings", "summary": "Reviewed PDF summary.", "publication_date": "2026-10-01"},
        body="Climate risk findings\nReviewed PDF summary.\nPublished: 2026-10-01")
    sha = _ingest(database, tmp_path, manifest)["candidate_sha256"]
    review = _review(database, tmp_path, sha)
    approve_owner_check(database, review, lifecycle_root=tmp_path/"runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    manifest = _date_manifest(database, source=source)
    with sqlite3.connect(database) as db:
        base = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
    view = _historical_projection(base, manifest["plan"]["baseline_reviews"])
    manifest["plan"].pop("date_semantics", None)
    manifest["plan"].pop("baseline_projection_version", None)
    manifest["plan"].update(baseline_public_projection_sha256=digest(view), nondate_scope_sha256=digest(dates._nondate(view)))
    run = dates._run(manifest)
    overlay = {"baseline_candidate_sha256": sha, "manifest": manifest, "run": run, "attempt": dates._attempt(manifest, run["run_id"])}
    snapshot = {**base, dates.OVERLAY: overlay}
    plan = manifest["plan"]
    receipt = {"schema_version": dates.DATE_REVIEW_VERSION, "review_scope": plan["scope"], "candidate_sha256": digest(snapshot),
        "source_checks": {"baseline_candidate_sha256": sha, "baseline_public_projection_sha256": plan["baseline_public_projection_sha256"],
            "nondate_scope_sha256": plan["nondate_scope_sha256"], "date_evidence_sha256": digest(manifest["observations"]),
            "attempt_id": overlay["attempt"]["attempt_id"], "packet_sha256": overlay["attempt"]["packet_sha256"]},
        "actor": {"id": "historical-reviewer", "task_id": "historical-independent-task"},
        "authorization_reference": "retained-date-authorization", "audit_reference": "retained-independent-review",
        "reviewed_at": "2026-10-16T12:01:00Z", "decision": "pass", "reason": "Inspected exact original capture and frozen inherited scope."}
    row = {"candidate_sha256": digest(snapshot), "status": "pass", "receipt_json": json.dumps(receipt),
        "review_id": digest([digest(snapshot), "pass", receipt])}
    return database, snapshot, row, view


def _retain_history(database, snapshot, review):
    sha = digest(snapshot)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO registry_candidates VALUES(?,?,?,?,?,?)", (sha, snapshot["entity_kind"], snapshot["entity_id"], json.dumps(snapshot), "2026-10-16T12:00:00Z", dates.DATE_CHECK_VERSION))
        db.execute("INSERT INTO registry_reviews VALUES(?,?,?,?,?)", (review["review_id"], sha, "pass", "2026-10-16T12:01:00Z", review["receipt_json"]))
        db.execute("UPDATE registry_publication SET published_candidate_sha256=?,latest_candidate_sha256=? WHERE entity_kind=? AND entity_id=?",
            (sha, sha, snapshot["entity_kind"], snapshot["entity_id"]))


@pytest.mark.parametrize("source", ["native", "pdf"])
def test_old_exact_projection_and_every_public_reader_keep_the_frozen_scope(tmp_path, monkeypatch, source):
    database, snapshot, review, old = _history(tmp_path, source)
    frozen = deepcopy(snapshot)
    view, _, _ = dates._frozen_scope(snapshot)
    assert digest(view) == digest(old)
    assert digest(dates._nondate(view)) == snapshot[dates.OVERLAY]["manifest"]["plan"]["nondate_scope_sha256"]
    verdict = evaluate_snapshot(snapshot, reviews=[review])
    assert verdict["eligible"]
    actual = permitted_view(snapshot, verdict)
    assert actual["derived_display"]["publication_date"] == "2026-10-02"
    assert digest(dates._nondate(actual)) == digest(dates._nondate(old))
    assert snapshot == frozen
    sha = digest(snapshot)
    # Retained historical rows, not a new writer approval or a rebound receipt.
    _retain_history(database, snapshot, review)
    with sqlite3.connect(database) as db:
        from climate_registry.display import refresh_display
        refresh_display(db)
    reader = _reader(database, tmp_path)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            item = reader.article("good") if source == "native" else reader.pdf_article("pdf-good")
            assert item["publication_date"] == "2026-10-02"
            assert item["published_candidate_sha256"] == sha
            assert item["summary"] == old["derived_display"]["summary"]
    import api_server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    api_server._clear_registry_list_cache()
    url = "/api/registry/articles/good" if source == "native" else "/api/registry/pdf-intake/articles/pdf-good"
    response = TestClient(api_server.app).get(url)
    assert response.status_code == 200
    assert response.json()["publication_date"] == "2026-10-02"
    assert response.json()["summary"] == old["derived_display"]["summary"]
    assert snapshot == frozen


def test_legacy_pdf_dto_cannot_reselect_a_later_owner_summary(tmp_path, monkeypatch):
    database, snapshot, review, old = _history(tmp_path, "pdf", later_owner=True)
    _retain_history(database, snapshot, review)
    verdict = evaluate_snapshot(snapshot, reviews=[review])
    assert verdict["eligible"]
    assert verdict["primary"]["occurrence_id"] == "occurrence"
    view = permitted_view(snapshot, verdict)
    assert view["derived_display"]["summary"] == "Original PDF observation"
    reader = _reader(database, tmp_path)
    with sqlite3.connect(database) as db:
        assert not reader._keeps_frozen_legacy_date_summary(db, view)
        assert reader._keeps_frozen_legacy_date_summary(db, view, public=True)
    with reader.public_snapshot(direct=True):
        actual = reader._pdf_article_from_snapshot(view)
        assert actual["summary"] == old["derived_display"]["summary"]
    with sqlite3.connect(database) as db:
        from climate_registry.display import refresh_display
        refresh_display(db)
        stored = json.loads(db.execute("SELECT dto FROM display_details WHERE kind='pdf_article' AND identity='pdf-good'").fetchone()[0])
        assert stored["summary"] == old["derived_display"]["summary"]
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            assert reader.pdf_article("pdf-good")["summary"] == old["derived_display"]["summary"]
    import api_server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    api_server._clear_registry_list_cache()
    response = TestClient(api_server.app).get("/api/registry/pdf-intake/articles/pdf-good")
    assert response.status_code == 200
    assert response.json()["summary"] == old["derived_display"]["summary"]


def test_current_date_scope_keeps_current_pdf_owner_selection(tmp_path):
    database, _, _, _ = _history(tmp_path, "pdf", later_owner=True)
    manifest = _date_manifest(database, source="pdf")
    staged = _ingest(database, tmp_path, manifest)
    receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
    assert _approve(database, tmp_path, receipt)["status"] == "approved"
    reader = _reader(database, tmp_path)
    with reader.public_snapshot(direct=True):
        assert reader.pdf_article("pdf-good")["summary"] == "Reviewed PDF summary."


@pytest.mark.parametrize("mutation", ["candidate", "baseline", "receipt", "actor", "task", "capture", "target", "date", "full_hash", "nondate_hash"])
def test_changed_historical_evidence_never_inherits_its_old_pass(tmp_path, mutation):
    _, snapshot, review, _ = _history(tmp_path, "native")
    if mutation == "receipt":
        review["receipt_json"] = json.dumps({**json.loads(review["receipt_json"]), "reason": "different receipt"})
    elif mutation == "candidate":
        snapshot["entity_id"] = "different"
    elif mutation == "baseline":
        snapshot[dates.OVERLAY]["baseline_candidate_sha256"] = "0"*64
    else:
        manifest = snapshot[dates.OVERLAY]["manifest"]
        entry = manifest["observations"][0]
        if mutation in {"actor", "task"}:
            manifest["actor"]["id" if mutation == "actor" else "task_id"] = json.loads(review["receipt_json"])["actor"]["id" if mutation == "actor" else "task_id"]
        elif mutation == "capture":
            entry["capture"]["text"] += "Changed source."
        elif mutation == "target":
            entry["target"]["entity_id"] = "unrelated"
        elif mutation == "date":
            entry["public_fields"]["publication_date"] = "2030-01-01"
        else:
            manifest["plan"]["baseline_public_projection_sha256" if mutation == "full_hash" else "nondate_scope_sha256"] = "0"*64
    verdict = evaluate_snapshot(snapshot, reviews=[review])
    assert not verdict["eligible"]
    assert permitted_view(snapshot, verdict) is None


def test_unknown_projection_and_new_scope_impersonation_fail_closed(tmp_path):
    _, snapshot, _, _ = _history(tmp_path, "native")
    snapshot[dates.OVERLAY]["manifest"]["plan"]["baseline_projection_version"] = "unknown-profile"
    with pytest.raises(ValueError, match="projection version"):
        dates._frozen_scope(snapshot)
    base, _ = dates._base(snapshot)
    base["enrichment_scope"] = {"schema_version": "registry-enrichment-scope.v1", "scope": "initial_article_content"}
    with pytest.raises(ValueError, match="legacy.*scope"):
        dates._baseline_view(base, [], projection_profile="registry-date-baseline.pre-g3.v1")


@pytest.mark.parametrize("key", ["id", "task_id"])
def test_independent_review_is_required_even_with_a_matching_receipt_hash(tmp_path, key):
    _, snapshot, row, _ = _history(tmp_path, "native")
    receipt = json.loads(row["receipt_json"])
    receipt["actor"][key] = snapshot[dates.OVERLAY]["manifest"]["actor"][key]
    row["receipt_json"] = json.dumps(receipt)
    row["review_id"] = digest([digest(snapshot), "pass", receipt])
    assert not evaluate_snapshot(snapshot, reviews=[row])["eligible"]


@pytest.mark.parametrize("mutation", ["current_version", "new_schema"])
def test_a_declared_version_cannot_downgrade_or_impersonate_the_old_scope(tmp_path, mutation):
    _, snapshot, _, _ = _history(tmp_path, "native")
    manifest = snapshot[dates.OVERLAY]["manifest"]
    if mutation == "current_version":
        manifest["plan"]["baseline_projection_version"] = "registry-date-baseline.current.v1"
    else:
        manifest["schema_version"] = "registry-enrichment-contract.v1"
    with pytest.raises(ValueError):
        dates._frozen_scope(snapshot)


def test_legacy_priority_preserves_the_current_unlinked_pdf_identity_guard(tmp_path):
    from climate_registry.publication import snapshot_entity
    database = _seed(tmp_path, source="same_url")
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", "good")
        pdf = snapshot_entity(db, "pdf_article", "pdf-good")
    # Old URL aggregation retained both observations without granting a core link.
    snapshot["information_targets"] += pdf["information_targets"]
    for section in ("tables", "dependencies"):
        for name, rows in pdf[section].items():
            target = snapshot[section].setdefault(name, [])
            target.extend(row for row in rows if row not in target)
    snapshot["tables"]["article_check_attempts"] = [row for row in snapshot["tables"]["article_check_attempts"] if row.get("occurrence_id") == "occurrence"]
    verdict = evaluate_snapshot(snapshot, projection_profile="registry-date-baseline.pre-g3.v1")
    assert any(row["verified"] and row["entity_kind"] == "pdf_article" for row in verdict["observations"].values())
    assert not verdict["eligible"] and verdict["primary"] is None


def test_new_date_writer_freezes_current_projection_version(tmp_path):
    database = _seed(tmp_path)
    manifest = _date_manifest(database)
    assert manifest["plan"]["baseline_projection_version"] == "registry-date-baseline.current.v1"
    staged = _ingest(database, tmp_path, manifest)
    receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
    assert _approve(database, tmp_path, receipt)["status"] == "approved"

@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("field", ["publication_date", "updated_date", "revision_date"])
def test_current_date_children_preserve_their_exact_approved_parent_summary(tmp_path, monkeypatch, legacy, field):
    from test_owner_publication_dates import _date_fields_manifest
    database, parent, parent_review, old = _history(tmp_path, "pdf", later_owner=True)
    if legacy:
        _retain_history(database, parent, parent_review)
    expected = old["derived_display"]["summary"] if legacy else "Reviewed PDF summary."
    with sqlite3.connect(database) as db:
        parent_sha = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='pdf_article' AND entity_id='pdf-good'").fetchone()[0]
        retained_parent = db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (parent_sha,)).fetchone()[0]
        parent_reviews = [dict(zip(("review_id", "candidate_sha256", "status", "reviewed_at", "receipt_json"), row)) for row in db.execute("SELECT * FROM registry_reviews WHERE candidate_sha256=?", (parent_sha,))]
    for generation in (1, 2):
        manifest = (_date_manifest(database, source="pdf", date="2026-10-0" + str(2 + generation))
            if field == "publication_date" else _date_fields_manifest(database, source="pdf", fields=(field,)))
        manifest["actor"] = {"id": "current-checker-" + str(generation), "task_id": "current-check-task-" + str(generation)}
        manifest["checked_at"] = "2026-10-" + str(16 + generation) + "T12:00:00Z"
        manifest["observations"][0]["capture"]["captured_at"] = "2026-10-" + str(16 + generation) + "T11:59:00Z"
        assert manifest["plan"]["baseline_projection_version"] == dates.BASELINE_PROJECTION
        staged = _ingest(database, tmp_path, manifest)
        receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
        receipt["reviewed_at"] = "2026-10-" + str(16 + generation) + "T12:01:00Z"
        with sqlite3.connect(database) as db:
            child = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (staged["candidate_sha256"],)).fetchone()[0])
            assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='pdf_article' AND entity_id='pdf-good'").fetchone()[0] == manifest["plan"]["baseline_candidate_sha256"]
            assert not evaluate_snapshot(child, reviews=parent_reviews)["eligible"]
        dates.validate_date_review(child, receipt)
        assert _approve(database, tmp_path, receipt)["status"] == "approved"
        reader = _reader(database, tmp_path)
        for direct in (True, False):
            with reader.public_snapshot(direct=direct):
                actual = reader.pdf_article("pdf-good")
                assert actual["summary"] == expected
                assert actual[field] == manifest["observations"][0]["public_fields"][field]
                assert actual["published_candidate_sha256"] == staged["candidate_sha256"]
        with sqlite3.connect(database) as db:
            stored = json.loads(db.execute("SELECT dto FROM display_details WHERE kind='pdf_article' AND identity='pdf-good'").fetchone()[0])
            assert stored["summary"] == expected
            assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (parent_sha,)).fetchone()[0] == retained_parent
        import api_server
        from fastapi.testclient import TestClient
        monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
        monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
        api_server._clear_registry_list_cache()
        response = TestClient(api_server.app).get("/api/registry/pdf-intake/articles/pdf-good")
        assert response.status_code == 200
        assert response.json()["summary"] == expected
        assert response.json()[field] == manifest["observations"][0]["public_fields"][field]

def _pending_legacy_child(tmp_path):
    database, parent, parent_review, _ = _history(tmp_path, "pdf", later_owner=True)
    _retain_history(database, parent, parent_review)
    manifest = _date_manifest(database, source="pdf", date="2026-10-03")
    staged = _ingest(database, tmp_path, manifest)
    receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
    with sqlite3.connect(database) as db:
        child = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (staged["candidate_sha256"],)).fetchone()[0])
    return database, parent, parent_review, child, receipt


@pytest.mark.parametrize("mutation", ["parent_summary", "parent_capture", "parent_receipt", "child_capture", "child_actor", "child_task", "child_target", "child_scope", "child_full_hash", "child_nondate_hash", "child_run", "child_attempt", "injected_profile"])
def test_current_child_cannot_inherit_changed_frozen_parent_or_child_proof(tmp_path, mutation):
    _, _, _, child, _ = _pending_legacy_child(tmp_path)
    overlay = child[dates.OVERLAY]
    manifest = overlay["manifest"]
    if mutation == "parent_summary":
        child["tables"]["pdf_intake_article_occurrences"][0]["occurrence_json"] += " "
    elif mutation == "parent_capture":
        overlay["inherited_date_overlay"]["manifest"]["observations"][0]["capture"]["text"] += "changed"
    elif mutation == "parent_receipt":
        manifest["plan"]["baseline_reviews"][0]["receipt_json"] += " "
    elif mutation == "injected_profile":
        child["_date_summary_projection"] = dates.LEGACY_BASELINE_PROJECTION
    elif mutation == "child_run":
        overlay["run"]["input_sha256"] = "0" * 64
    elif mutation == "child_attempt":
        overlay["attempt"]["packet_sha256"] = "0" * 64
    elif mutation in {"child_actor", "child_task"}:
        manifest["actor"]["id" if mutation == "child_actor" else "task_id"] += "changed"
    elif mutation in {"child_full_hash", "child_nondate_hash"}:
        manifest["plan"]["baseline_public_projection_sha256" if mutation == "child_full_hash" else "nondate_scope_sha256"] = "0" * 64
    elif mutation == "child_scope":
        manifest["plan"]["scope"] = "complete_article_content_update"
    else:
        entry = manifest["observations"][0]
        if mutation == "child_capture":
            entry["capture"]["text"] += "changed"
        else:
            entry["target"]["entity_id"] = "other-record"
    with pytest.raises(ValueError):
        dates._frozen_scope(child)
    assert not evaluate_snapshot(child, reviews=[])["eligible"]


@pytest.mark.parametrize("mutation", ["parent_receipt", "rebound_parent_receipt", "missing", "scope", "actor", "task", "time", "capture", "review_hash"])
def test_current_child_requires_its_own_fresh_exact_receipt(tmp_path, mutation):
    from climate_registry.errors import RegistryInputError
    database, parent, parent_review, child, receipt = _pending_legacy_child(tmp_path)
    sha = digest(child)
    if mutation in {"parent_receipt", "rebound_parent_receipt"}:
        receipt = json.loads(parent_review["receipt_json"])
        if mutation == "rebound_parent_receipt":
            receipt["candidate_sha256"] = sha
    elif mutation == "scope":
        receipt["review_scope"] = dates.DATE_FIELDS_SCOPE
    elif mutation in {"actor", "task"}:
        key = "id" if mutation == "actor" else "task_id"
        receipt["actor"][key] = child[dates.OVERLAY]["manifest"]["actor"][key]
    elif mutation == "time":
        receipt["reviewed_at"] = "2026-10-15T12:00:00Z"
    elif mutation == "capture":
        receipt["source_checks"]["date_evidence_sha256"] = "0" * 64
    row = {"candidate_sha256": sha, "status": "pass", "review_id": digest([sha, "pass", receipt]), "receipt_json": json.dumps(receipt)}
    if mutation == "review_hash":
        row["review_id"] = "0" * 64
    assert not evaluate_snapshot(child, reviews=[] if mutation == "missing" else [row])["eligible"]
    if mutation not in {"missing", "review_hash"}:
        with pytest.raises(RegistryInputError):
            _approve(database, tmp_path, receipt)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='pdf_article' AND entity_id='pdf-good'").fetchone()[0] == digest(parent)
        assert not db.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=?", (sha,)).fetchone()
    reader = _reader(database, tmp_path)
    assert reader.pdf_article("pdf-good")["summary"] == "Original PDF observation"


@pytest.mark.parametrize("pointer", ["latest", "published", "visibility"])
def test_current_child_approval_remains_bound_to_the_active_parent_pointer(tmp_path, pointer):
    from climate_registry.errors import RegistryInputError
    database, parent, _, child, receipt = _pending_legacy_child(tmp_path)
    with sqlite3.connect(database) as db:
        if pointer == "latest":
            db.execute("UPDATE registry_publication SET latest_candidate_sha256=? WHERE entity_kind='pdf_article' AND entity_id='pdf-good'", (digest(parent),))
        elif pointer == "published":
            db.execute("UPDATE registry_publication SET published_candidate_sha256=? WHERE entity_kind='pdf_article' AND entity_id='pdf-good'", (parent[dates.OVERLAY]["baseline_candidate_sha256"],))
        else:
            db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_kind='pdf_article' AND entity_id='pdf-good'")
        before = db.execute("SELECT * FROM registry_publication WHERE entity_kind='pdf_article' AND entity_id='pdf-good'").fetchone()
    message = "selected complete owner scope or required dependency is held or invalid" if pointer == "latest" else "binding changed"
    with pytest.raises(RegistryInputError, match=message):
        _approve(database, tmp_path, receipt)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM registry_publication WHERE entity_kind='pdf_article' AND entity_id='pdf-good'").fetchone() == before
        assert not db.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=?", (digest(child),)).fetchone()


@pytest.mark.parametrize("scope", ["source_activity", "human_date", "human_activity"])
def test_legacy_summary_inheritance_keeps_activity_and_human_authority_separate(tmp_path, monkeypatch, scope):
    from test_owner_activity_dates import _manifest as activity_manifest, _human_activity
    from test_article_content_dates import _manual
    database, parent, parent_review, _ = _history(tmp_path, "pdf", later_owner=True)
    _retain_history(database, parent, parent_review)
    manifest = (activity_manifest(database, source="pdf") if scope == "source_activity" else
        _human_activity(database, source="pdf") if scope == "human_activity" else _manual(database, source="pdf"))
    staged = _ingest(database, tmp_path, manifest)
    if scope == "source_activity":
        receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
        assert _approve(database, tmp_path, receipt)["status"] == "approved"
    else:
        assert staged["status"] == "approved"
        assert staged["authorization_basis"] == dates.MANUAL_AUTHORIZATION
    reader = _reader(database, tmp_path)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.pdf_article("pdf-good")
            assert actual["summary"] == "Original PDF observation"
            if scope == "human_date":
                assert actual["publication_date_verification"]["authority"] == "human_override"
                assert "reviewer" not in actual["publication_date_verification"]
            elif scope == "human_activity":
                assert actual["activity_dates"][0]["authority"] == "human_override"
                assert actual["activity_dates"][0]["verification_status"] == "unchecked"
            else:
                assert actual["is_activity"] and actual["activity_dates"]
    with sqlite3.connect(database) as db:
        stored = json.loads(db.execute("SELECT dto FROM display_details WHERE kind='pdf_article' AND identity='pdf-good'").fetchone()[0])
        assert stored["summary"] == "Original PDF observation"
    import api_server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    api_server._clear_registry_list_cache()
    response = TestClient(api_server.app).get("/api/registry/pdf-intake/articles/pdf-good")
    assert response.status_code == 200
    assert response.json()["summary"] == "Original PDF observation"