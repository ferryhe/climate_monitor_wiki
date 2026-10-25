"""G8 handoffs preserve exact review authority and isolate complete items."""
import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from climate_registry import item_lifecycle as lifecycle
from climate_registry.acquisition_review import digest
from climate_registry.publication import stage_entities, prepare_review, _approve, stage_snapshot
from climate_registry.owner_checks import prepare_owner_checks, ingest_owner_check
from climate_registry.errors import RegistryInputError
from test_registry_publication import _database, _complete_fixture_information, _approve_native_packet


def _candidates(database):
    with sqlite3.connect(database) as connection:
        return dict(connection.execute("SELECT entity_id,latest_candidate_sha256 FROM registry_publication"))


def _hold(root, database, sha):
    return lifecycle.record_dispute(root, database, sha, text="Select the intended record",
        evidence=["retained source identity comparison"], options=["investigate", "keep"],
        impact="Only this candidate and its actual version-dependent Dates items wait")


def _reply(dispute, sha, *, identity="a", decision_id="reply-1"):
    return {"decision_id": decision_id, "kind": "business", "actor": "actual-owner",
        "reference": "owner-message:1", "received_at": "2026-10-25T12:00:00Z",
        "text": "Investigate this record", "target": {"entity_kind": "article", "entity_id": identity},
        "candidate_sha256": sha, "dispute_id": dispute["dispute_id"], "choice": "investigate"}


def _date_reply(database, *, decision_id="date-1", source="native"):
    from test_article_content_dates import _manual
    manifest = _manual(database, source=source)
    return {"decision_id": decision_id, "kind": "human_date", "actor": manifest["manual_authorization"]["authorized_by"],
        "reference": manifest["authorization_reference"], "received_at": "2026-10-25T12:00:00Z",
        "text": manifest["manual_authorization"]["instruction"],
        "target": {key: manifest["plan"][key] for key in ("entity_kind", "entity_id")}, "manifest": manifest}


def test_one_held_of_one_hundred_does_not_block_actual_native_approval(tmp_path, monkeypatch):
    database = _database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    with sqlite3.connect(database) as connection:
        for index in range(98):
            identity = f"independent-{index:02d}"
            connection.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES(?,?,'s','2026-09-01','2026-09-01')",
                (identity, "https://example.org/" + identity))
            connection.execute("INSERT INTO article_versions VALUES(?,?,?,?,?,?,?,'2026-09-01','2026-09-01')",
                ("v-" + identity, identity, "Title", "Title", "Observed summary", "f-" + identity, "report-title-summary"))
            connection.execute("UPDATE articles SET current_version_id=? WHERE article_id=?", ("v-" + identity, identity))
    _complete_fixture_information(database)
    root = tmp_path / "runs"
    blocked = _candidates(database)["a"]
    _hold(root, database, blocked)
    packet = prepare_review(database, root / "registry-review")
    assert len(packet["candidates"]) == 99
    assert all(item["entity_id"] != "a" for item in packet["candidates"])
    _approve_native_packet(database, root / "registry-review", tmp_path / "hermes.db", "cron_g8_99")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM registry_publication WHERE published_candidate_sha256 IS NOT NULL").fetchone()[0] == 99
        assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='a'").fetchone()[0] is None
    assert lifecycle.item_status(root, database, candidate_sha256s=[blocked])["held"][0]["reason"] == "awaiting_actual_reply"


def test_real_article_dates_dependency_waits_without_holding_siblings(tmp_path, monkeypatch):
    from test_issue197_workflow import _native_meeting_material_fixture
    database, _, process, _ = _native_meeting_material_fixture(tmp_path, monkeypatch)
    process("first")
    _complete_fixture_information(database)
    root = tmp_path / "runs"
    with sqlite3.connect(database) as connection:
        article = connection.execute("SELECT article_id FROM climate_event_sources LIMIT 1").fetchone()[0]
    _hold(root, database, _candidates(database)[article])
    status = lifecycle.item_status(root, database)
    meeting = next(item for item in status["held"] if item["entity_kind"] == "meeting")
    assert meeting["reason"] == "required_article_content_version_not_ready"
    assert meeting["dependency"]["event_source_id"]
    assert not prepare_review(database, root / "registry-review")["candidates"]


def test_dispute_dedup_no_reply_and_exact_business_reply_do_not_approve(tmp_path):
    database = _database(tmp_path)
    _complete_fixture_information(database)
    root = tmp_path / "runs"
    shas = _candidates(database)
    first = _hold(root, database, shas["a"])
    assert first["created"] is True
    assert _hold(root, database, shas["a"]) == {**first, "created": False}
    other = _hold(root, database, shas["b"])
    before = database.read_bytes()
    assert lifecycle.item_status(root, database)["ready"] == []
    with pytest.raises(ValueError, match="actual decision"):
        lifecycle.import_decisions(root, database, [])
    reply = _reply(first, shas["a"])
    assert lifecycle.import_decisions(root, database, [reply])["decisions"][0]["status"] == "recorded"
    raw = (root / "item-lifecycle/state.json").read_bytes()
    assert lifecycle.import_decisions(root, database, [reply])["decisions"][0]["status"] == "no-op"
    assert (root / "item-lifecycle/state.json").read_bytes() == raw
    assert lifecycle.resume_items(root, database, decision_id="reply-1")["execution"] == "not_dispatched"
    status = lifecycle.item_status(root, database)
    assert [item["entity_id"] for item in status["ready"]] == ["a"]
    assert status["held"][0]["dispute_id"] == other["dispute_id"]
    assert database.read_bytes() == before
    with pytest.raises(ValueError, match="reused"):
        lifecycle.import_decisions(root, database, [{**reply, "text": "Different answer"}])


def test_changed_business_inputs_do_not_rebind_old_reply(tmp_path):
    database = _database(tmp_path)
    _complete_fixture_information(database)
    root = tmp_path / "runs"
    old = _candidates(database)["a"]
    dispute = _hold(root, database, old)
    lifecycle.import_decisions(root, database, [_reply(dispute, old)])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE articles SET last_seen='2026-10-25' WHERE article_id='a'")
        stage_entities(connection)
    current = _candidates(database)["a"]
    assert current != old
    result = lifecycle.item_status(root, database, candidate_sha256s=[current, old])
    assert result["held"][0]["reason"] == "dispute_inputs_changed"
    assert result["invalid"][0]["candidate_sha256"] == old
    assert result["decisions"]["reply-1"]["reply"]["candidate_sha256"] == old


def test_current_replied_dispute_replaces_historical_applicability_hold(tmp_path):
    database = _database(tmp_path)
    _complete_fixture_information(database)
    root = tmp_path / "runs"
    old = _candidates(database)["a"]
    first = _hold(root, database, old)
    original_reply = _reply(first, old)
    lifecycle.import_decisions(root, database, [original_reply])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE articles SET last_seen='2026-10-25' WHERE article_id='a'")
        stage_entities(connection)
    current = _candidates(database)["a"]
    assert lifecycle.item_status(root, database, candidate_sha256s=[current])["held"][0]["reason"] == "dispute_inputs_changed"
    second = _hold(root, database, current)
    waiting = lifecycle.item_status(root, database, candidate_sha256s=[current])["held"][0]
    assert waiting["reason"] == "awaiting_actual_reply" and waiting["dispute_id"] == second["dispute_id"]
    actual_reply = _reply(second, current, decision_id="reply-current")
    lifecycle.import_decisions(root, database, [actual_reply])
    status = lifecycle.item_status(root, database, candidate_sha256s=[current])
    assert status["held"] == [] and status["ready"][0]["business_decisions"] == ["reply-current"]
    assert status["decisions"]["reply-1"]["reply"] == original_reply
    assert status["disputes"][first["dispute_id"]]["candidate_sha256"] == old
    before = (root / "item-lifecycle/state.json").read_bytes()
    lifecycle.import_decisions(root, database, [actual_reply])
    assert (root / "item-lifecycle/state.json").read_bytes() == before


def test_preparation_rolls_back_entire_bad_item_and_default_staging_stays_strict(tmp_path, monkeypatch):
    import climate_registry.publication as publication
    database = _database(tmp_path)
    _complete_fixture_information(database)
    original = publication.stage_snapshot
    with sqlite3.connect(database) as connection:
        old = _candidates(database)["a"]
        count = connection.execute("SELECT count(*) FROM registry_candidates").fetchone()[0]
    def malformed(connection, snapshot, **kwargs):
        if snapshot["entity_id"] == "a":
            snapshot["derived_display"] = {"summary": "PARTIAL PRIVATE VALUE"}
            original(connection, snapshot, **kwargs)
            raise ValueError("malformed complete candidate")
        return original(connection, snapshot, **kwargs)
    monkeypatch.setattr(publication, "stage_snapshot", malformed)
    packet = prepare_review(database, tmp_path / "runs/registry-review")
    assert [item["entity_id"] for item in packet["candidates"]] == ["b"]
    with sqlite3.connect(database) as connection:
        assert _candidates(database)["a"] == old
        assert connection.execute("SELECT count(*) FROM registry_candidates").fetchone()[0] == count
        with pytest.raises(ValueError, match="malformed"):
            stage_entities(connection)


def test_owner_preparation_filters_complete_manifests_and_keeps_atomic_ingest(tmp_path):
    from test_verified_publication import _database as owner_database
    from test_owner_checks import _manifest
    database = owner_database(tmp_path)
    manifests = [_manifest(database), _manifest(database, identity="unchecked", occurrence="core:unchecked")]
    bad = deepcopy(manifests)
    bad[1]["plan"]["expected_state"]["is_visible"] = 0
    before = database.read_bytes()
    result = prepare_owner_checks(database, bad, root=tmp_path / "runs")
    assert result["ready"] == manifests[:1] and len(result["invalid"]) == 1
    assert database.read_bytes() == before
    with pytest.raises(RegistryInputError, match="stale"):
        ingest_owner_check(database, bad, queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups", apply=True)
    assert database.read_bytes() == before
    result = ingest_owner_check(database, result["ready"], queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups", apply=True)
    assert len(result["results"]) == 1
    assert result["results"][0]["fact_publication"] == "awaiting_independent_owner_review"


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
def test_explicit_date_import_is_private_and_resume_uses_g1_once(tmp_path, source):
    from test_owner_publication_dates import _seed
    from test_verified_publication import _reader
    database = _seed(tmp_path, source)
    root = tmp_path / "runs"
    reply = _date_reply(database, source=source)
    before = database.read_bytes()
    lifecycle.import_decisions(root, database, [reply])
    assert database.read_bytes() == before
    result = lifecycle.resume_items(root, database, decision_id="date-1", queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups")
    assert result["result"]["status"] == "approved" and result["downstream"] == "not_dispatched"
    reader = _reader(database, tmp_path)
    article = reader.pdf_article("pdf-good") if source == "pdf" else reader.article("good")
    override = article["manual_date_overrides"]["publication_date"]
    assert override["new_value"] == "2026-10-02" and "source_effective" in override
    assert "reason" not in reply["manifest"]["manual_authorization"]
    after = database.read_bytes()
    assert lifecycle.resume_items(root, database, decision_id="date-1")["status"] == "no-op"
    assert database.read_bytes() == after


def test_date_receipt_recovers_after_external_state_write_loss(tmp_path, monkeypatch):
    from test_owner_publication_dates import _seed
    database = _seed(tmp_path)
    root = tmp_path / "runs"
    lifecycle.import_decisions(root, database, [_date_reply(database)])
    save = lifecycle._save
    def lose_result(root, state):
        if state["decisions"]["date-1"].get("result"):
            raise OSError("result disk unavailable")
        save(root, state)
    monkeypatch.setattr(lifecycle, "_save", lose_result)
    with pytest.raises(OSError, match="unavailable"):
        lifecycle.resume_items(root, database, decision_id="date-1", queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups")
    monkeypatch.setattr(lifecycle, "_save", save)
    before = database.read_bytes()
    result = lifecycle.resume_items(root, database, decision_id="date-1", queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups")
    assert result["status"] == "recovered" and result["result"]["review_id"]
    assert database.read_bytes() == before


def test_explicit_date_uses_original_instruction_after_unrelated_candidate_change(tmp_path):
    from test_owner_publication_dates import _seed
    from test_verified_publication import _reader
    database = _seed(tmp_path)
    root = tmp_path / "runs"
    reply = _date_reply(database)
    lifecycle.import_decisions(root, database, [reply])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE articles SET last_seen='2026-10-25' WHERE article_id='good'")
        stage_entities(connection)
    result = lifecycle.resume_items(root, database, decision_id="date-1", queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups")
    assert result["result"]["status"] == "approved"
    saved = lifecycle.item_status(root, database)["decisions"]["date-1"]
    assert saved["reply"] == reply
    assert saved["execution_manifests"][0]["plan"] != reply["manifest"]["plan"]
    assert saved["execution_manifests"][0]["manual_authorization"] == reply["manifest"]["manual_authorization"]
    assert _reader(database, tmp_path).article("good")["manual_date_overrides"]["publication_date"]["new_value"] == "2026-10-02"


def test_later_explicit_date_authority_holds_only_conflicting_instruction(tmp_path):
    from test_owner_publication_dates import _seed
    from test_article_content_dates import _manual
    from test_owner_checks import _ingest
    database = _seed(tmp_path)
    root = tmp_path / "runs"
    lifecycle.import_decisions(root, database, [_date_reply(database)])
    _ingest(database, tmp_path, _manual(database, fields={"publication_date": "2026-10-03"}))
    before = database.read_bytes()
    result = lifecycle.resume_items(root, database, decision_id="date-1", queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups")
    assert result["status"] == "held" and "later human" in result["reason"]
    assert database.read_bytes() == before


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
@pytest.mark.parametrize("field", ["publication_date", "updated_date", "revision_date"])
def test_delayed_date_instruction_respects_committed_set_then_revoke(tmp_path, source, field):
    from test_owner_publication_dates import _seed
    from test_article_content_dates import _manual
    from test_owner_checks import _ingest
    from test_verified_publication import _reader
    database = _seed(tmp_path, source)
    root = tmp_path / "runs"
    reply = _date_reply(database, source=source)
    reply["manifest"]["checked_at"] = reply["received_at"]
    lifecycle.import_decisions(root, database, [reply])
    later = _manual(database, source=source, fields={field: "2026-10-03"})
    later.update(checked_at="2026-10-25T13:00:00Z", authorization_reference="human-message:later-set")
    _ingest(database, tmp_path, later)
    revoke = _manual(database, source=source, fields={field: "2026-10-03"})
    revoke.update(checked_at="2026-10-25T14:00:00Z", authorization_reference="human-message:later-revoke")
    revoke["manual_authorization"].update(action="revoke", instruction=f"Revoke the {field} override.")
    revoke["observations"][0].update(public_fields={field: None}, field_evidence={field: {"text": "revoke", "precision": "unknown"}})
    revoked = _ingest(database, tmp_path, revoke)
    reader = _reader(database, tmp_path)
    read = lambda: reader.pdf_article("pdf-good") if source == "pdf" else reader.article("good")
    before_public = read()
    assert field not in before_public.get("manual_date_overrides", {})
    before = database.read_bytes()
    files = lambda: {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    handoffs = files()
    result = lifecycle.resume_items(root, database, decision_id="date-1", queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups")
    if field == "publication_date":
        assert result["status"] == "held" and result["next_owner"] == "owner" and "later human" in result["reason"]
        assert database.read_bytes() == before and files() == handoffs
        assert read() == before_public
        assert read()["published_candidate_sha256"] == revoked["candidate_sha256"]
    else:
        assert result["status"] == "executed" and result["result"]["status"] == "approved"
        after = read()
        assert after["manual_date_overrides"]["publication_date"]["instruction_reference"] == reply["reference"]
        assert after["publication_date"] == "2026-10-02"
        assert field not in after["manual_date_overrides"] and after.get(field) == before_public.get(field)


@pytest.mark.parametrize("later_field", ["activity_dates", "publication_date"])
def test_delayed_activity_instruction_respects_only_its_committed_authority_events(tmp_path, later_field):
    from test_owner_publication_dates import _seed
    from test_owner_activity_dates import _human_activity
    from test_article_content_dates import _manual
    from test_owner_checks import _ingest
    from test_verified_publication import _reader
    database = _seed(tmp_path)
    root = tmp_path / "runs"
    original = _human_activity(database)
    original["checked_at"] = "2026-10-25T12:00:00Z"
    reply = {"decision_id": "activity-1", "kind": "human_date", "actor": original["manual_authorization"]["authorized_by"],
        "reference": original["authorization_reference"], "received_at": original["checked_at"],
        "text": original["manual_authorization"]["instruction"],
        "target": {key: original["plan"][key] for key in ("entity_kind", "entity_id")}, "manifest": original}
    lifecycle.import_decisions(root, database, [reply])
    later = _human_activity(database, start="2026-11-05") if later_field == "activity_dates" else _manual(database)
    later.update(checked_at="2026-10-25T13:00:00Z", authorization_reference="human-message:later-field-set")
    _ingest(database, tmp_path, later)
    revoke = _human_activity(database) if later_field == "activity_dates" else _manual(database)
    revoke.update(checked_at="2026-10-25T14:00:00Z", authorization_reference="human-message:later-field-revoke")
    revoke["manual_authorization"].update(action="revoke", instruction=f"Revoke {later_field}.")
    revoke["observations"][0].update(public_fields={later_field: None}, field_evidence={later_field: {"text": "revoke", "precision": "unknown"}})
    _ingest(database, tmp_path, revoke)
    reader = _reader(database, tmp_path)
    before, public = database.read_bytes(), reader.article("good")
    result = lifecycle.resume_items(root, database, decision_id="activity-1", queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups")
    if later_field == "activity_dates":
        assert result["status"] == "held" and database.read_bytes() == before and reader.article("good") == public
    else:
        assert result["status"] == "executed" and result["result"]["status"] == "approved"
        after = reader.article("good")
        assert after["publication_date"] == public["publication_date"]
        assert after["manual_date_overrides"]["activity_dates"]["instruction_reference"] == reply["reference"]


def test_date_during_pending_b_preserves_private_content_and_obsolete_b_cannot_replay(tmp_path):
    from test_enrichment_contract import _published_a, _stage_update
    from test_verified_publication import _reader
    database, a = _published_a(tmp_path)
    b, frozen = _stage_update(database)
    root = tmp_path / "runs"
    lifecycle.import_decisions(root, database, [_date_reply(database)])
    result = lifecycle.resume_items(root, database, decision_id="date-1", queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups")
    a_prime = result["result"]["candidate_sha256"]
    status = lifecycle.item_status(root, database)
    assert any(item["candidate_sha256"] == b for item in status["invalid"])
    resumed = lifecycle.resume_items(root, database)["items"]
    assert any(item["candidate_sha256"] == b for item in resumed["invalid"])
    assert "20 billion" not in _reader(database, tmp_path).article("good")["summary"]
    with sqlite3.connect(database) as connection:
        approved_snapshot = json.loads(connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (a_prime,)).fetchone()[0])
        assert next(row["current_content_version_id"] for row in approved_snapshot["tables"]["articles"] if row["article_id"] == "good") == "body-a"
    with sqlite3.connect(database) as connection:
        b_prime = stage_snapshot(connection, deepcopy(frozen))
        snapshot = json.loads(connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (b_prime,)).fetchone()[0])
        assert snapshot["derived_display"]["manual_date_overrides"]["publication_date"]["new_value"] == "2026-10-02"
        assert snapshot["enrichment_scope"]["content_comparison_baseline"]["candidate_sha256"] == a
        assert snapshot["enrichment_scope"]["approval_inheritance_baseline"]["candidate_sha256"] == a_prime
        assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == a_prime
        assert not connection.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=? AND status='pass'", (b_prime,)).fetchone()
    c, _ = _stage_update(database, version="body-c")
    with sqlite3.connect(database) as connection:
        _approve(connection, c, {"basis": "explicit isolated new-content fixture approval"})
        with pytest.raises(ValueError, match="content comparison baseline mismatch"):
            stage_snapshot(connection, deepcopy(frozen))
    assert lifecycle.resume_items(root, database)["execution"] == "not_dispatched"
    assert _reader(database, tmp_path).article("good")["content_version_id"] == "body-c"


def test_approval_rollback_is_not_approved_and_downstream_failure_does_not_reapprove(tmp_path, monkeypatch):
    from test_owner_publication_dates import _seed
    import climate_registry.display as display
    from climate_registry.pdf_pipeline import PdfIntakePipeline
    database = _seed(tmp_path)
    root, queue, runtime = tmp_path / "runs", tmp_path / "queue", tmp_path / "runtime"
    lifecycle.import_decisions(root, database, [_date_reply(database)])
    refresh = display.refresh_display
    monkeypatch.setattr(display, "refresh_display", lambda *_: (_ for _ in ()).throw(RuntimeError("display failed")))
    with pytest.raises(RuntimeError, match="display failed"):
        lifecycle.resume_items(root, database, decision_id="date-1", queue_dir=queue, backup_dir=tmp_path / "backups")
    assert "result" not in lifecycle.item_status(root, database)["decisions"]["date-1"]
    monkeypatch.setattr(display, "refresh_display", refresh)
    result = lifecycle.resume_items(root, database, decision_id="date-1", queue_dir=queue, backup_dir=tmp_path / "backups")
    approved = result["result"]["candidate_sha256"]
    application = tmp_path / "application"; application.mkdir()
    def failed_reload(_):
        raise TimeoutError("actual fixture reload failure")
    writer = PdfIntakePipeline(queue, database, tmp_path / "backups", runtime, failed_reload, repository_root=application)
    failed = writer.refresh_publication()
    assert failed["stage"] == "failed" and failed["prepared_generation"]
    status = lifecycle.item_status(root, database, candidate_sha256s=[approved], queue_dir=queue, runtime_wiki_dir=runtime)
    delivery = status["ready"][0]["delivery"]
    assert delivery["stage"] == "prepared" and delivery["retry_after"] == failed["retry_after"]
    assert delivery["verification"]["status"] == "unknown"
    before = database.read_bytes()
    assert lifecycle.resume_items(root, database, decision_id="date-1")["status"] == "no-op"
    assert database.read_bytes() == before
    # Reuse the actual prepared generation through the writer's existing retry path.
    from climate_delivery.io import atomic_write_json
    def acknowledge(generation):
        pending = json.loads((queue / "pending.json").read_text())
        assert pending["generation_id"] == generation
        atomic_write_json(queue / "active.json", pending)
    writer.reload_chat = acknowledge
    writer.refresh_checks("exact-existing-prepared-retry")
    active = lifecycle.item_status(root, database, candidate_sha256s=[approved], queue_dir=queue, runtime_wiki_dir=runtime)["ready"][0]["delivery"]
    assert active["stage"] == "active" and active["verification"]["status"] == "unknown"


def test_status_export_cli_are_read_only_and_resume_never_claims_dispatch(tmp_path, capsys):
    from scripts.review_pipeline import main
    database = _database(tmp_path)
    _complete_fixture_information(database)
    root = tmp_path / "runs"
    args = ["--kind", "acquisition", "--root", str(root), "--database", str(database)]
    before = database.read_bytes()
    assert main(["status", *args]) == 0
    status = json.loads(capsys.readouterr().out)
    assert len(status["ready"]) == 2 and not root.exists()
    assert main(["export", *args]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["handoff_sha256"] == digest(status) and exported["notification_status"] == "not_sent"
    assert main(["resume", *args]) == 0
    assert json.loads(capsys.readouterr().out)["execution"] == "not_dispatched"
    assert database.read_bytes() == before and not root.exists()


def test_empty_packet_discovery_is_read_only_and_claim_freezes_only_ready_scope(tmp_path, monkeypatch, capsys):
    from scripts.review_pipeline import main, pending
    from climate_registry.publication import claim_registry_review
    from climate_delivery.io import transaction_lock
    from climate_delivery.errors import LockStateError
    from test_issue197_workflow import _native
    database = _database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    _complete_fixture_information(database)
    root = tmp_path / "runs"
    shas = _candidates(database)
    a = _hold(root, database, shas["a"])
    _hold(root, database, shas["b"])
    assert prepare_review(database, root / "registry-review")["candidates"] == []
    old_packet = (root / "registry-review/packet.json").read_bytes()
    lifecycle.import_decisions(root, database, [_reply(a, shas["a"])])
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    raw = database.read_bytes()
    selected = pending("acquisition", root)[1]
    assert selected["preparation_required"] and selected["ready_candidate_sha256s"] == [shas["a"]]
    assert main(["peek", "--kind", "acquisition", "--root", str(root)]) == 0
    peek = json.loads(capsys.readouterr().out)
    assert peek["preparation_required"] and peek["ready_candidate_sha256s"] == [shas["a"]]
    assert peek["resolved_paths"] == {} and peek["packet"]["candidates"] == []
    assert peek["verification_report"]["status"] == "written"
    after = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert all(after[path] == content for path, content in before.items())
    assert {Path(path).relative_to(root).parts[:2] for path in after.keys() - before.keys()} == {
        ("registry-review", "verification-reports")}
    assert database.read_bytes() == raw
    native = tmp_path / "native.db"
    _native(native, "cron_ready_owner")
    context = dict(session_id="cron_ready_owner", execution_id="ready", reviewer="independent-native", hermes_database=native)
    with transaction_lock(root / "registry-review", "review"):
        with pytest.raises(LockStateError, match="owned"):
            claim_registry_review(root / "registry-review", **context)
    assert (root / "registry-review/packet.json").read_bytes() == old_packet
    assert main(["claim", "--kind", "acquisition", "--root", str(root), "--target", "registry-review",
        "--session-id", "cron_ready_owner", "--execution-id", "ready", "--reviewer", "independent-native",
        "--hermes-database", str(native)]) == 0
    claimed = json.loads(capsys.readouterr().out)
    assert [item["entity_id"] for item in claimed["packet"]["candidates"]] == ["a"]
    assert claimed["packet_sha256"] == digest(claimed["packet"])
    assert any(path.read_bytes() == old_packet for path in (root / "registry-review/packets").glob("*.json"))
    frozen = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    _native(native, "cron_other_owner")
    with pytest.raises(ValueError, match="owned"):
        claim_registry_review(root / "registry-review", **{**context, "session_id": "cron_other_owner"})
    assert frozen == {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_new_acquisition_article_stages_before_queue_and_retains_exact_native_gate(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from climate_delivery.io import atomic_write_json
    from climate_registry import acquisition_review as review
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    from climate_registry.read_api import RegistryReader, RegistryNotFoundError
    from test_issue112_acquisition import _batch, _item
    from test_issue197_workflow import _native, _native_read
    database = _database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    payload = _batch([_item(url="https://fresh.example.net/new-g8", discovery_kind="site",
        discovery_ref="site:new-g8", discovery_search_ref=None)], searches=[],
        search_decision={"status": "no_search", "reason": "website only"})
    store_acquisition_batch(database, payload)
    item = load_acquisition_batch(database, payload["batch_id"])["items"][0]
    new_id = item["article_id"]
    with sqlite3.connect(database) as connection:
        staged = connection.execute("SELECT latest_candidate_sha256,published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id=?", (new_id,)).fetchone()
    assert staged[0] and staged[1] is None
    runs = tmp_path / "runs"; run = runs / "new-run"
    binding = {"registry_database": str(database), "run_id": "new-run", "acquisition_batch_id": payload["batch_id"],
        "attempt": 1, "task_version": 3, "source_keys": [item["source_name"]]}
    atomic_write_json(run / "binding.json", binding)
    atomic_write_json(run / "attempt-1-acquisition.json", payload)
    atomic_write_json(run / "attempt-1-result.json", {"run_id": "new-run", "attempt": 1, "finished_at": "2026-10-25T12:00:00Z"})
    state = review.queue_acquisition_review(run / "binding.json", binding, payload)
    assert state["packet"]["candidates"] == []
    assert state["excluded_candidates"][0]["registry_candidate_sha256"] == staged[0]
    root = run / "acquisition-review"
    with pytest.raises(ValueError, match="completed source-bound T1"):
        review.claim_acquisition(root, session_id="cron_unready_new", execution_id="unready", reviewer="native", hermes_database=tmp_path / "absent.db")
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    with pytest.raises(RegistryNotFoundError):
        reader.article(new_id)
    _complete_fixture_information(database, entity_ids={new_id})
    state = review.queue_acquisition_review(run / "binding.json", binding, payload)
    candidate = state["packet"]["candidates"][0]
    assert candidate["identity"] == item and candidate["registry_candidate_sha256"]
    historical = deepcopy(state["packet"])
    historical["candidates"][0] = {key: value for key, value in candidate.items() if not key.startswith("registry_")}
    historical["candidates"][0]["candidate_sha256"] = digest(item)
    before = database.read_bytes()
    with pytest.raises(ValueError, match="historical source-only candidate"):
        review.claim_review(root, historical, session_id="cron_historical", execution_id="old", reviewer="native", hermes_database=tmp_path / "absent.db")
    assert database.read_bytes() == before and not (root / "claim.json").exists()
    native = tmp_path / "native.db"; session = "cron_new_article"
    _native(native, session)
    _native_read(native, session, root / candidate["body_path"], item["markdown_content"], "new-body")
    path = Path(candidate["registry_snapshot_path"])
    _native_read(native, session, path, path.read_text(encoding="utf-8"), "new-snapshot")
    at = datetime(2026, 10, 25, 12, 5, tzinfo=timezone.utc)
    claim = review.claim_acquisition(root, session_id=session, execution_id="new", reviewer="native", hermes_database=native, now=at)
    with pytest.raises(RegistryNotFoundError):
        reader.article(new_id)
    assert review.activate_approved(root, queue_dir=tmp_path / "queue", database=database, repository_root=tmp_path / "application")["status"] == "no_approved_candidates"
    result = review.review_acquisition(root, claim["token"], {"items": {item["acquisition_item_id"]: {
        "candidate_sha256": candidate["candidate_sha256"], "status": "pass", "reason": "Inspected this exact new article body and Registry snapshot"}}}, now=at)
    assert result["item_reviews"][item["acquisition_item_id"]]["raw_candidate_sha256"] == digest(item)
    assert reader.article(new_id)["published_candidate_sha256"] == candidate["registry_candidate_sha256"]
    request = review.activate_approved(root, queue_dir=tmp_path / "queue", database=database, repository_root=tmp_path / "application")
    assert [value["acquisition_item_id"] for value in request["web_items"]] == [item["acquisition_item_id"]]


def _source_review_candidate(tmp_path, monkeypatch):
    from climate_delivery.io import atomic_write_json
    from climate_registry import acquisition_review as review
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    from test_issue112_acquisition import _batch, _item
    from test_issue197_workflow import _native, _native_read
    database = _database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    payload = _batch([_item(url="https://fresh.example.net/negative-g8", discovery_kind="site",
        discovery_ref="site:negative-g8", discovery_search_ref=None)], searches=[],
        search_decision={"status": "no_search", "reason": "website only"})
    store_acquisition_batch(database, payload)
    item = load_acquisition_batch(database, payload["batch_id"])["items"][0]
    _complete_fixture_information(database)
    runs = tmp_path / "runs"; run = runs / "source-run"
    binding = dict(registry_database=str(database), run_id="source-run", acquisition_batch_id=payload["batch_id"],
        attempt=1, task_version=3, source_keys=[item["source_name"]])
    atomic_write_json(run / "binding.json", binding)
    atomic_write_json(run / "attempt-1-acquisition.json", payload)
    atomic_write_json(run / "attempt-1-result.json", dict(run_id="source-run", attempt=1, finished_at="2026-10-25T12:00:00Z"))
    state = review.queue_acquisition_review(run / "binding.json", binding, payload)
    candidate = state["packet"]["candidates"][0]
    root = run / "acquisition-review"; native = tmp_path / "native.db"; session = "cron_negative_source"
    _native(native, session)
    _native_read(native, session, root / candidate["body_path"], item["markdown_content"], "negative-body")
    snapshot = Path(candidate["registry_snapshot_path"])
    _native_read(native, session, snapshot, snapshot.read_text(encoding="utf-8"), "negative-snapshot")
    claim = review.claim_acquisition(root, session_id=session, execution_id="negative", reviewer="native", hermes_database=native)
    return database, runs, root, item, candidate, claim, native


@pytest.mark.parametrize("verdict", ["needs_correction", "rejected"])
def test_actual_acquisition_failure_survives_requeue_and_new_candidate_resets(tmp_path, monkeypatch, verdict):
    from climate_registry import acquisition_review as review
    database, runs, root, item, candidate, claim, _ = _source_review_candidate(tmp_path, monkeypatch)
    sha = candidate["registry_candidate_sha256"]
    result = review.review_acquisition(root, claim["token"], {"items": {item["acquisition_item_id"]: {
        "candidate_sha256": candidate["candidate_sha256"], "status": verdict, "reason": "Return this exact summary to its author"}}})
    assert result["item_reviews"][item["acquisition_item_id"]]["status"] == verdict
    receipt_path, = (root / "receipts").glob("*.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    for _ in range(2):
        status = lifecycle.item_status(runs, database)
        held = next(value for value in status["held"] if value["candidate_sha256"] == sha)
        assert held["stage"] == "needs_correction" and held["next_owner"] == "author"
        assert held["reviews"] == []  # A source failure is not a fabricated Registry review.
        fact, = held["acquisition_reviews"]
        assert fact["status"] == verdict and fact["candidate_sha256"] == candidate["candidate_sha256"]
        assert fact["claim"] == claim and fact["receipt_sha256"] == digest(receipt)
        assert fact["registry_candidate_sha256"] == sha and fact["acquisition_item_id"] == item["acquisition_item_id"]
        assert {value["entity_id"] for value in status["ready"]} >= {"a", "b"}
        packet = prepare_review(database, runs / "registry-review")
        assert sha not in {value["candidate_sha256"] for value in packet["candidates"]}
        assert {value["entity_id"] for value in packet["candidates"]} >= {"a", "b"}
    assert json.loads((root / "state.json").read_text())["item_reviews"] == {}
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM registry_reviews WHERE candidate_sha256=?", (sha,)).fetchall() == []
        assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id=?", (item["article_id"],)).fetchone()[0] is None
        connection.execute("UPDATE articles SET last_seen='2026-10-26' WHERE article_id=?", (item["article_id"],))
        stage_entities(connection)
    fresh_sha = _candidates(database)[item["article_id"]]
    assert fresh_sha != sha
    fresh, = lifecycle.item_status(runs, database, candidate_sha256s=[fresh_sha])["ready"]
    assert fresh["stage"] == "awaiting_independent_review" and fresh["acquisition_reviews"] == []


@pytest.mark.parametrize("verdict", ["needs_correction", "rejected"])
def test_excluded_source_failure_can_be_corrected_through_cli_and_fresh_review(tmp_path, monkeypatch, capsys, verdict):
    from climate_registry import acquisition_review as review
    from climate_registry.read_api import RegistryReader, RegistryNotFoundError
    from scripts.review_pipeline import main
    from test_issue197_workflow import _native, _native_read
    database, runs, root, item, candidate, claim, native = _source_review_candidate(tmp_path, monkeypatch)
    item_id, old_sha = item["acquisition_item_id"], candidate["registry_candidate_sha256"]
    review.review_acquisition(root, claim["token"], {"items": {item_id: {
        "candidate_sha256": candidate["candidate_sha256"], "status": verdict, "reason": "Correct the unsupported search summary"}}})
    receipts = {path: path.read_bytes() for path in (root / "receipts").glob("*.json")}
    old_packet = root / "packets" / (claim["packet_sha256"] + ".json")
    old_packet_bytes = old_packet.read_bytes()
    # Ordinary preparation refreshes the source queue and keeps the exact failed item excluded.
    _approve_native_packet(database, runs / "registry-review", tmp_path / "siblings-native.db", "cron_independent_siblings")
    review.refresh_acquisition_reviews(database, runs)
    excluded_state = json.loads((root / "state.json").read_text())
    assert excluded_state["packet"]["candidates"] == []
    excluded, = excluded_state["excluded_candidates"]
    assert excluded["identity"] == item and excluded["registry_candidate_sha256"] == old_sha
    with sqlite3.connect(database) as connection:
        before_public = dict(connection.execute("SELECT entity_id,published_candidate_sha256 FROM registry_publication"))
    assert before_public["a"] and before_public["b"] and before_public[item["article_id"]] is None
    correction = tmp_path / "correction.json"
    correction.write_text(json.dumps({"item_id": item_id, "changes": {"summary": "Evidence."},
        "reason": "Use the actual retained body instead of the search excerpt"}))
    author = "cron_actual_source_corrector"
    _native(native, author)
    assert main(["correct", "--kind", "acquisition", "--root", str(runs), "--target", root.parent.name,
        "--result", str(correction), "--session-id", author, "--execution-id", "correction",
        "--hermes-database", str(native)]) == 0
    corrected = json.loads(capsys.readouterr().out)
    changed, = corrected["excluded_candidates"]
    assert corrected["packet"]["candidates"] == []
    assert changed["identity"] == item and changed["registry_candidate_sha256"] != old_sha
    assert changed["candidate_sha256"] != candidate["candidate_sha256"] and changed["revision"] == 2
    assert changed["display"] == {"summary": "Evidence."}
    assert corrected["packet"]["excluded_candidate_sha256s"] == [changed["registry_candidate_sha256"]]
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    with pytest.raises(RegistryNotFoundError):
        reader.article(item["article_id"])
    with sqlite3.connect(database) as connection:
        assert dict(connection.execute("SELECT entity_id,published_candidate_sha256 FROM registry_publication")) == before_public
        assert not connection.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=?", (changed["registry_candidate_sha256"],)).fetchone()
    # The usual T1/prepare/claim path determines readiness; correction itself grants no approval.
    _complete_fixture_information(database)
    prepare_review(database, runs / "registry-review")
    ready_state = json.loads((root / "state.json").read_text())
    fresh, = ready_state["packet"]["candidates"]
    assert fresh["identity"] == item and fresh["registry_candidate_sha256"] != old_sha
    assert fresh["display"] == changed["display"] and fresh["revision"] == 2
    assert lifecycle.item_status(runs, database, candidate_sha256s=[fresh["registry_candidate_sha256"]])["ready"]
    with pytest.raises(ValueError, match="claim"):
        review.review_acquisition(root, claim["token"], {"items": {item_id: {
            "candidate_sha256": candidate["candidate_sha256"], "status": "pass", "reason": "Stale prior review"}}})
    with pytest.raises(ValueError, match="independent cron"):
        review.claim_acquisition(root, session_id=author, execution_id="same-author", reviewer="native", hermes_database=native)
    session = "cron_independent_corrected_source"
    _native(native, session)
    _native_read(native, session, root / fresh["body_path"], item["markdown_content"], "corrected-body")
    snapshot = Path(fresh["registry_snapshot_path"])
    _native_read(native, session, snapshot, snapshot.read_text(encoding="utf-8"), "corrected-snapshot")
    ownership = review.claim_acquisition(root, session_id=session, execution_id="fresh", reviewer="native", hermes_database=native)
    result = review.review_acquisition(root, ownership["token"], {"items": {item_id: {
        "candidate_sha256": fresh["candidate_sha256"], "status": "pass", "reason": "Independently inspected the corrected candidate and retained body"}}})
    assert result["item_reviews"][item_id]["status"] == "pass"
    assert reader.article(item["article_id"])["published_candidate_sha256"] == fresh["registry_candidate_sha256"]
    assert result["item_reviews"][item_id]["display"] == {"summary": "Evidence."}
    with sqlite3.connect(database) as connection:
        assert lifecycle._snapshot(connection, fresh["registry_candidate_sha256"])["derived_display"] == {"summary": "Evidence."}
    assert old_packet.read_bytes() == old_packet_bytes
    assert all(path.read_bytes() == content for path, content in receipts.items())
    failures, = review._negative_review_receipts(runs, database)[old_sha]
    assert failures["status"] == verdict and failures["candidate_sha256"] == candidate["candidate_sha256"]
    with sqlite3.connect(database) as connection:
        assert {key: value for key, value in connection.execute("SELECT entity_id,published_candidate_sha256 FROM registry_publication") if key != item["article_id"]} == {
            key: value for key, value in before_public.items() if key != item["article_id"]}


@pytest.mark.parametrize("invalid", ["missing", "cli_missing", "missing_execution", "missing_database", "mismatch", "unknown_session", "non_cron", "wrong_database", "unreadable_database"])
def test_correction_requires_actual_native_identity_before_any_write(tmp_path, monkeypatch, invalid):
    from climate_registry import acquisition_review as review
    from scripts.review_pipeline import main
    from test_issue197_workflow import _native
    database, runs, root, item, candidate, claim, native = _source_review_candidate(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    author = "cron_actual_correction_author"
    _native(native, author)
    context = dict(session_id=author, execution_id="correction", hermes_database=native)
    if invalid in {"missing", "cli_missing"}:
        context = {}
    elif invalid == "missing_execution":
        context.pop("execution_id")
    elif invalid == "missing_database":
        context.pop("hermes_database")
    elif invalid == "mismatch":
        monkeypatch.setenv("HERMES_SESSION_ID", claim["session_id"])
    elif invalid == "unknown_session":
        context["session_id"] = "cron_unrecorded_author"
    elif invalid == "non_cron":
        with sqlite3.connect(native) as connection:
            connection.execute("UPDATE sessions SET source='cli' WHERE id=?", (author,))
    elif invalid == "wrong_database":
        other = tmp_path / "other-native.db"
        _native(other, "cron_another_context")
        context["hermes_database"] = other
    elif invalid == "unreadable_database":
        context["hermes_database"] = tmp_path / "absent-native.db"
    correction = tmp_path / "correction.json"
    correction.write_text(json.dumps({"item_id": item["acquisition_item_id"], "changes": {"summary": "Evidence."}, "reason": "Actual correction"}))
    before = database.read_bytes()
    files = lambda: {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    original = files()
    with pytest.raises(ValueError, match="native|identity"):
        if invalid == "cli_missing":
            main(["correct", "--kind", "acquisition", "--root", str(runs), "--target", root.parent.name, "--result", str(correction)])
        else:
            review.correct_candidate(root, item["acquisition_item_id"], {"summary": "Evidence."}, reason="Actual correction", **context)
    assert database.read_bytes() == before and files() == original


@pytest.mark.parametrize("published", [False, True])
def test_retained_correction_rejects_real_capture_body_drift_without_writes(tmp_path, monkeypatch, published):
    from climate_registry import acquisition_review as review
    from climate_registry.capture import capture_enrich_registry
    from scripts.review_pipeline import main
    from test_climate_registry_capture import FakeTransport, _response
    from test_issue197_workflow import _native
    database, runs, root, item, candidate, claim, native = _source_review_candidate(tmp_path, monkeypatch)
    final_root = runs / "registry-review"
    if published:
        historical_packet = _approve_native_packet(database, final_root, tmp_path / "public-native.db", "cron_public_original_a")
        historical_packet = json.loads((final_root / "packet.json").read_text())
    else:
        historical_packet = prepare_review(database, final_root)
    original_sha = candidate["registry_candidate_sha256"]
    review.review_acquisition(root, claim["token"], {"items": {item["acquisition_item_id"]: {
        "candidate_sha256": candidate["candidate_sha256"], "status": "needs_correction", "reason": "Correct retained body A summary"}}})
    with sqlite3.connect(database) as connection:
        original_public = connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id=?",
            (item["article_id"],)).fetchone()[0]
    assert original_public == (original_sha if published else None)
    result = capture_enrich_registry(database, tmp_path / "capture-backups", article_ids=[item["article_id"]], refresh=True,
        resolver=lambda host, port: ["93.184.216.34"],
        transport=FakeTransport(_response(body=b"<html><body><h1>Different source body B</h1><p>Climate insurance flood findings changed.</p></body></html>")),
        clock=lambda: "2026-10-26T12:00:00Z")
    assert result["status"] == "updated"
    with sqlite3.connect(database) as connection:
        current_body = connection.execute("SELECT current_content_version_id FROM articles WHERE article_id=?", (item["article_id"],)).fetchone()[0]
    assert current_body and current_body != item["content_version_id"]
    author = "cron_stale_original_a_author"
    _native(native, author)
    monkeypatch.setenv("HERMES_SESSION_ID", author)
    correction = tmp_path / "correction.json"
    correction.write_text(json.dumps({"item_id": item["acquisition_item_id"], "changes": {"summary": "Summary for old A."},
        "reason": "Correct the retained A packet"}))
    files = lambda: {path: path.read_bytes() for path in runs.rglob("*") if path.is_file()}
    before, handoffs = database.read_bytes(), files()
    with pytest.raises(ValueError, match="correction.*(stale|scope|body)"):
        main(["correct", "--kind", "acquisition", "--root", str(runs), "--target", root.parent.name,
            "--result", str(correction), "--session-id", author, "--execution-id", "stale-correction", "--hermes-database", str(native)])
    assert database.read_bytes() == before and files() == handoffs
    with sqlite3.connect(database) as connection:
        assert not connection.execute("SELECT 1 FROM knowledge_versions WHERE json_extract(evidence_json,'$.correction_context.session_id')=?", (author,)).fetchone()
        assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id=?", (item["article_id"],)).fetchone()[0] == original_public
        for (encoded,) in connection.execute("SELECT snapshot_json FROM registry_candidates WHERE entity_id=?", (item["article_id"],)):
            assert json.loads(encoded).get("derived_display", {}).get("summary") != "Summary for old A."
    _complete_fixture_information(database)
    current_packet = prepare_review(database, final_root)
    assert original_sha not in {row["candidate_sha256"] for row in current_packet["candidates"]}
    with pytest.raises(ValueError, match="completed source-bound T1|held|changed"):
        review.claim_review(final_root, historical_packet, session_id=author, execution_id="stale-final",
            reviewer="native", hermes_database=native)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id=?", (item["article_id"],)).fetchone()[0] == original_public


def test_unchanged_correction_cannot_replace_immutable_actual_author(tmp_path, monkeypatch):
    from climate_registry import acquisition_review as review
    from test_issue197_workflow import _native
    database, runs, root, item, candidate, claim, native = _source_review_candidate(tmp_path, monkeypatch)
    for author in ("cron_author_a", "cron_author_b"):
        _native(native, author)
    review.correct_candidate(root, item["acquisition_item_id"], {"summary": "Evidence."}, reason="Actual correction",
        session_id="cron_author_a", execution_id="a", hermes_database=native)
    before = database.read_bytes()
    files = lambda: {path: path.read_bytes() for path in root.rglob("*") if path.is_file() and path.name != ".review.lock"}
    original = files()
    with pytest.raises(ValueError, match="actual author binding"):
        review.correct_candidate(root, item["acquisition_item_id"], {"summary": "Evidence."}, reason="Same fields from another context",
            session_id="cron_author_b", execution_id="b", hermes_database=native)
    assert database.read_bytes() == before and files() == original


@pytest.mark.parametrize("route", ["claim", "submit", "source_claim", "source_submit"])
@pytest.mark.parametrize("legacy", [False, "missing", "reviewer_only"])
def test_corrected_source_requires_another_context_on_final_registry_route(tmp_path, monkeypatch, route, legacy):
    from climate_registry import acquisition_review as review
    from climate_registry.publication import claim_registry_review, review_candidates
    from test_issue197_workflow import _native, _native_read
    from scripts.review_pipeline import main
    from contextlib import redirect_stdout
    import io
    database, runs, root, item, original, claim, native = _source_review_candidate(tmp_path, monkeypatch)
    review.review_acquisition(root, claim["token"], {"items": {item["acquisition_item_id"]: {
        "candidate_sha256": original["candidate_sha256"], "status": "needs_correction", "reason": "Correct this exact search summary"}}})
    receipts = {path: path.read_bytes() for path in (root / "receipts").glob("*.json")}
    final_root = runs / "registry-review"
    prepare_review(database, final_root)
    author = "cron_actual_correction_author"
    _native(native, author)
    correction = tmp_path / "correction.json"
    correction.write_text(json.dumps({"item_id": item["acquisition_item_id"],
        "changes": {"summary": "Evidence."}, "reason": "Use the retained body"}))
    with monkeypatch.context() as setup:
        if legacy:
            from climate_registry import publication
            snapshot_entity = publication.snapshot_entity
            def historical_snapshot(*args, **kwargs):
                snapshot = snapshot_entity(*args, **kwargs)
                for row in snapshot["tables"].get("knowledge_versions", []):
                    evidence = json.loads(row["evidence_json"])
                    if "correction_context" in evidence:
                        if legacy == "missing":
                            evidence.pop("correction_context")
                        else:
                            evidence["correction_context"] = {key: value for key, value in evidence["correction_context"].items()
                                if key not in {"execution_id", "hermes_database"}}
                            evidence["correction_context"]["session_id"] = claim["session_id"]
                        row["evidence_json"] = json.dumps(evidence)
                return snapshot
            monkeypatch.setattr(publication, "snapshot_entity", historical_snapshot)
        setup.setenv("HERMES_SESSION_ID", author)
        with redirect_stdout(io.StringIO()):
            assert main(["correct", "--kind", "acquisition", "--root", str(runs), "--target", root.parent.name,
                "--result", str(correction), "--session-id", author, "--execution-id", "actual-author",
                "--hermes-database", str(native)]) == 0
        if legacy:
            # Old correct stored the released reviewer as parent and no actual-author history.
            historical = json.loads((root / "state.json").read_text())
            historical["packet"]["parent_context"] = claim["session_id"]
            historical["history"][-1].pop("correction_context")
            historical["packet_sha256"] = digest(historical["packet"])
            review.atomic_write_json(root / "packets" / (historical["packet_sha256"] + ".json"), historical["packet"])
            review.atomic_write_json(root / "state.json", historical)
        _complete_fixture_information(database)
        packet = prepare_review(database, final_root)
    candidate = next(row for row in packet["candidates"] if row["entity_id"] == item["article_id"])
    sha = candidate["candidate_sha256"]
    assert sha != original["registry_candidate_sha256"]
    with sqlite3.connect(database) as connection:
        frozen = lifecycle._snapshot(connection, sha)
    assert frozen.get("derived_display", {}).get("summary") == "Evidence.", frozen.get("derived_display")
    source_state = json.loads((root / "state.json").read_text())
    source_candidate, = source_state["packet"]["candidates"]
    # The released prior review is historical evidence, never the current author.
    assert json.loads((root / "claim.json").read_text())["session_id"] == claim["session_id"] != author
    assert source_state["packet"]["parent_context"] == (claim["session_id"] if legacy else author)
    if not legacy:
        with sqlite3.connect(database) as connection:
            corrections = [json.loads(row["evidence_json"])["correction_context"]
                for row in lifecycle._snapshot(connection, sha)["tables"]["knowledge_versions"]
                if "correction_context" in json.loads(row["evidence_json"])]
        assert corrections[-1] == {"session_id": author, "execution_id": "actual-author", "hermes_database": str(native.resolve()),
            "item_id": item["acquisition_item_id"], "content_version_id": item["content_version_id"], "display": {"summary": "Evidence."}}
    for row in packet["candidates"]:
        path = Path(row["snapshot_path"])
        _native_read(native, author, path, path.read_text(), row["candidate_sha256"])
    conclusions = {sha: {"candidate_sha256": sha, "enrichment_contract_sha256": candidate["enrichment_contract_sha256"],
        "status": "pass", "reason": "Read the complete corrected candidate"}}
    context = dict(session_id=author, execution_id="same-corrector", reviewer="native", hermes_database=native)
    _native_read(native, author, root / source_candidate["body_path"], item["markdown_content"], "author-body")
    snapshot = Path(source_candidate["registry_snapshot_path"])
    _native_read(native, author, snapshot, snapshot.read_text(), "author-source-snapshot")
    if route == "submit":
        # Model an already-issued historical claim; restore the real gate before submission.
        with monkeypatch.context() as setup:
            setup.setattr(lifecycle, "_require_correction_independence", lambda *args: None, raising=False)
            historical = claim_registry_review(final_root, **context)
    elif route == "source_submit":
        # A claim issued before the new author gate must fail at the real submit gate too.
        with monkeypatch.context() as setup:
            setup.setattr(lifecycle, "_require_correction_independence", lambda *args: None)
            historical = review.claim_review(root, source_state["packet"], **context)
    before = database.read_bytes()
    with pytest.raises(ValueError, match="independent cron context|actual native author identity"):
        if route == "claim":
            claim_registry_review(final_root, **context)
        elif route == "submit":
            review_candidates(final_root, historical["token"], conclusions)
        elif route == "source_claim":
            review.claim_acquisition(root, **context)
        else:
            review.review_acquisition(root, historical["token"], {"items": {item["acquisition_item_id"]: {
                "candidate_sha256": source_candidate["candidate_sha256"], "status": "pass", "reason": "Self review"}}})
    assert database.read_bytes() == before and _candidates(database)[item["article_id"]] == sha
    assert all(path.read_bytes() == content for path, content in receipts.items())
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id=?", (item["article_id"],)).fetchone()[0] is None
    with sqlite3.connect(native) as connection:
        connection.execute("UPDATE sessions SET end_reason='cron_completed' WHERE id=?", (author,))
    independent = "cron_final_independent_correction"
    _native(native, independent)
    for row in packet["candidates"]:
        path = Path(row["snapshot_path"])
        _native_read(native, independent, path, path.read_text(), row["candidate_sha256"])
    if legacy:
        # An old reviewer-only packet cannot establish who actually authored its correction.
        with pytest.raises(ValueError, match="actual native author identity"):
            claim_registry_review(final_root, session_id=independent, execution_id="independent", reviewer="native", hermes_database=native)
        assert database.read_bytes() == before
        return
    fresh = claim_registry_review(final_root, session_id=independent, execution_id="independent", reviewer="native", hermes_database=native)
    assert review_candidates(final_root, fresh["token"], conclusions)["reviewed"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id=?", (item["article_id"],)).fetchone()[0] == sha
    assert all(path.read_bytes() == content for path, content in receipts.items())


def test_source_failure_receipt_blocks_already_claimed_final_packet_after_state_write_loss(tmp_path, monkeypatch):
    from climate_registry import acquisition_review as review
    from climate_registry.publication import review_candidates
    from test_issue197_workflow import _native, _native_read
    database, runs, root, item, candidate, claim, native = _source_review_candidate(tmp_path, monkeypatch)
    final_root = runs / "registry-review"; packet = prepare_review(database, final_root)
    session = "cron_final_before_source_failure"
    _native(native, session)
    for value in packet["candidates"]:
        path = Path(value["snapshot_path"])
        _native_read(native, session, path, path.read_text(encoding="utf-8"), value["candidate_sha256"])
    final_claim = review.claim_review(final_root, packet, session_id=session, execution_id="final", reviewer="native", hermes_database=native)
    original = review.atomic_write_json
    def lose_state(path, value):
        if Path(path) == root / "state.json":
            raise OSError("lost state response after immutable receipt")
        return original(path, value)
    monkeypatch.setattr(review, "atomic_write_json", lose_state)
    with pytest.raises(OSError, match="lost state response"):
        review.review_acquisition(root, claim["token"], {"items": {item["acquisition_item_id"]: {
            "candidate_sha256": candidate["candidate_sha256"], "status": "needs_correction", "reason": "Return this exact summary"}}})
    assert json.loads((root / "state.json").read_text())["item_reviews"] == {}
    held, = lifecycle.item_status(runs, database, candidate_sha256s=[candidate["registry_candidate_sha256"]])["held"]
    assert held["stage"] == "needs_correction" and held["next_owner"] == "author"
    # A previously issued final claim cannot erase a later exact source failure.
    before = database.read_bytes()
    with pytest.raises(ValueError, match="held|correction"):
        review_candidates(final_root, final_claim["token"], {value["candidate_sha256"]: {
            "candidate_sha256": value["candidate_sha256"], "status": "pass", "reason": "Full snapshot read"} for value in packet["candidates"]})
    assert database.read_bytes() == before
    assert json.loads((final_root / "packet.json").read_text()) == packet


def test_source_failure_receipt_uses_existing_registry_writer_lock_and_retries_exact_claim(tmp_path, monkeypatch):
    from climate_registry import acquisition_review as review
    from climate_registry.persistent import _exclusive_database_lock
    from climate_registry.errors import RegistryLockError
    database, runs, root, item, candidate, claim, _ = _source_review_candidate(tmp_path, monkeypatch)
    conclusions = {"items": {item["acquisition_item_id"]: {"candidate_sha256": candidate["candidate_sha256"],
        "status": "rejected", "reason": "This exact content needs correction"}}}
    before = (root / "state.json").read_bytes()
    with _exclusive_database_lock(database):
        with pytest.raises(RegistryLockError, match="locked"):
            review.review_acquisition(root, claim["token"], conclusions)
    assert not list((root / "receipts").glob("*.json"))
    assert (root / "state.json").read_bytes() == before
    review.review_acquisition(root, claim["token"], conclusions)
    receipt_path, = (root / "receipts").glob("*.json")
    frozen = receipt_path.read_bytes()
    with pytest.raises(ValueError, match="claim"):
        review.review_acquisition(root, claim["token"], conclusions)
    assert list((root / "receipts").glob("*.json")) == [receipt_path] and receipt_path.read_bytes() == frozen
    assert lifecycle.item_status(runs, database, candidate_sha256s=[candidate["registry_candidate_sha256"]])["held"][0]["next_owner"] == "author"


def test_acquisition_rechecks_hold_after_precheck_before_writer_and_retries_same_claim(tmp_path, monkeypatch):
    from climate_registry import acquisition_review as review
    from climate_registry.read_api import RegistryReader
    from test_issue197_workflow import _native, _native_read
    database, runs, root, item, candidate, claim, native = _source_review_candidate(tmp_path, monkeypatch)
    def submit(selected, ownership):
        return review.review_acquisition(root, ownership["token"], {"items": {item["acquisition_item_id"]: {
            "candidate_sha256": selected["candidate_sha256"], "status": "pass", "reason": "Inspected exact body and Registry scope"}}})
    submit(candidate, claim)
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    old_public = reader.article(item["article_id"])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE articles SET last_seen='2026-10-27' WHERE article_id=?", (item["article_id"],))
    run = root.parent
    binding = json.loads((run / "binding.json").read_text())
    payload = json.loads((run / "attempt-1-acquisition.json").read_text())
    state = review.queue_acquisition_review(run / "binding.json", binding, payload)
    candidate, = state["packet"]["candidates"]
    sha = candidate["registry_candidate_sha256"]
    assert sha != old_public["published_candidate_sha256"]
    session = "cron_acquisition_after_precheck"
    _native(native, session)
    _native_read(native, session, root / candidate["body_path"], item["markdown_content"], "race-body")
    path = Path(candidate["registry_snapshot_path"])
    _native_read(native, session, path, path.read_text(encoding="utf-8"), "race-snapshot")
    claim = review.claim_acquisition(root, session_id=session, execution_id="race", reviewer="native", hermes_database=native)
    original, disputes = review.read_verified_text, []
    def hold_after_precheck(*args, **kwargs):
        inspected = original(*args, **kwargs)
        if not disputes:
            disputes.append(_hold(runs, database, sha))
            held, = lifecycle.item_status(runs, database, candidate_sha256s=[sha])["held"]
            assert held["stage"] == "owner_choice" and held["reason"] == "awaiting_actual_reply"
        return inspected
    monkeypatch.setattr(review, "read_verified_text", hold_after_precheck)
    database_before = database.read_bytes()
    state_before = (root / "state.json").read_bytes()
    claim_before = (root / "claim.json").read_bytes()
    receipts_before = {path: path.read_bytes() for path in (root / "receipts").glob("*.json")}
    try:
        result = submit(candidate, claim)
    except ValueError as exc:
        assert "held" in str(exc)
    else:
        pytest.fail(json.dumps({"unexpected": result["item_reviews"][item["acquisition_item_id"]]["status"],
            "old_public_sha": old_public["published_candidate_sha256"], "held_sha": sha,
            "actual_public_sha": reader.article(item["article_id"])["published_candidate_sha256"],
            "decisions": lifecycle._load(runs, database)["decisions"]}))
    assert len(disputes) == 1 and database.read_bytes() == database_before
    assert reader.article(item["article_id"]) == old_public
    assert (root / "state.json").read_bytes() == state_before and (root / "claim.json").read_bytes() == claim_before
    assert {path: path.read_bytes() for path in (root / "receipts").glob("*.json")} == receipts_before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM registry_reviews WHERE candidate_sha256=?", (sha,)).fetchall() == []
    lifecycle.import_decisions(runs, database, [_reply(disputes[0], sha, identity=item["article_id"])])
    assert submit(candidate, claim)["item_reviews"][item["acquisition_item_id"]]["status"] == "pass"
    assert reader.article(item["article_id"])["published_candidate_sha256"] == sha
    receipts = {path: path.read_bytes() for path in (root / "receipts").glob("*.json")}
    assert len(receipts) == len(receipts_before) + 1
    with pytest.raises(ValueError, match="claim"):
        submit(candidate, claim)
    assert {path: path.read_bytes() for path in (root / "receipts").glob("*.json")} == receipts


@pytest.mark.parametrize("verdict", ["needs_correction", "rejected"])
def test_status_preserves_exact_failed_review_stage(tmp_path, verdict):
    database = _database(tmp_path)
    _complete_fixture_information(database)
    sha = _candidates(database)["a"]
    with sqlite3.connect(database) as connection:
        review_id = _approve(connection, sha, {"basis": "exact isolated failed review fixture"}, status=verdict)
    status = lifecycle.item_status(tmp_path / "runs", database, candidate_sha256s=[sha])
    item = next(item for group in ("ready", "held", "invalid") for item in status[group])
    assert item["reviews"][0]["review_id"] == review_id
    assert item["stage"] == "needs_correction" and item["next_owner"] == "author"
    assert status["held"] == [item] and item["reason"] == "exact_candidate_review_" + verdict
    with sqlite3.connect(database) as connection:
        latest = _approve(connection, sha, {"basis": "a later exact failed review"},
            status="needs_correction" if verdict == "rejected" else "rejected")
    current = lifecycle.item_status(tmp_path / "runs", database, candidate_sha256s=[sha])["held"][0]
    assert current["review_id"] == latest and current["reviews"][-1]["review_id"] == latest
    assert current["reviews"][0]["review_id"] == review_id
    assert not prepare_review(database, tmp_path / "runs/registry-review")["candidates"] or all(
        item["candidate_sha256"] != sha for item in prepare_review(database, tmp_path / "runs/registry-review")["candidates"])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE articles SET last_seen='2026-10-25' WHERE article_id='a'")
        stage_entities(connection)
    new_sha = _candidates(database)["a"]
    assert new_sha != sha
    fresh = lifecycle.item_status(tmp_path / "runs", database, candidate_sha256s=[new_sha])["ready"][0]
    assert fresh["stage"] == "awaiting_independent_review" and fresh["reviews"] == []


def test_owner_approval_rechecks_post_ingest_dispute_atomically_and_preserves_public_baseline(tmp_path, capsys):
    from climate_registry.owner_checks import approve_owner_check
    from test_verified_publication import _database as owner_database, _reader
    from test_owner_checks import _manifest, _review
    database = owner_database(tmp_path)
    queue, backups, root = tmp_path / "queue", tmp_path / "backups", tmp_path / "runs"
    original = _manifest(database, fields={"title": "Climate risk findings", "summary": "Earlier supported flood findings."},
        body="Climate risk findings\nEarlier supported flood findings.")
    baseline = ingest_owner_check(database, original, queue_dir=queue, backup_dir=backups, apply=True)
    approve_owner_check(database, _review(database, tmp_path, baseline["candidate_sha256"]), lifecycle_root=tmp_path / "runs", queue_dir=queue, backup_dir=backups, apply=True)
    old = _reader(database, tmp_path).article("good")
    manifests = [_manifest(database), _manifest(database, identity="unchecked", occurrence="core:unchecked")]
    for manifest in manifests:
        manifest["checked_at"] = "2026-10-16T23:00:00Z"
        manifest["observations"][0]["capture"]["captured_at"] = "2026-10-16T22:59:00Z"
    prepared = prepare_owner_checks(database, manifests, root=root)
    assert prepared["ready"] == manifests
    assert prepared["lifecycle_root"] == str(root.resolve()) and prepared["registry_database"] == str(database.resolve())
    staged = ingest_owner_check(database, prepared["ready"], queue_dir=queue, backup_dir=backups, apply=True)
    sha = staged["results"][0]["candidate_sha256"]
    dispute = _hold(root, database, sha)
    receipts = [_review(database, tmp_path, value["candidate_sha256"]) for value in staged["results"]]
    for receipt in receipts:
        receipt["reviewed_at"] = "2026-10-17T00:00:00Z"
    before, backup_files = database.read_bytes(), list(backups.iterdir())
    try:
        approved = approve_owner_check(database, receipts, lifecycle_root=tmp_path / "runs", queue_dir=queue, backup_dir=backups, apply=True)
    except RegistryInputError as exc:
        assert "held" in str(exc) or "dispute" in str(exc)
    else:
        pytest.fail(json.dumps({"unexpected": approved["status"], "old_public_sha": old["published_candidate_sha256"],
            "held_sha": sha, "actual_public_sha": _reader(database, tmp_path).article("good")["published_candidate_sha256"],
            "decisions": lifecycle._load(root, database)["decisions"]}))
    assert database.read_bytes() == before and list(backups.iterdir()) == backup_files
    assert _reader(database, tmp_path).article("good") == old
    assert [value["article_id"] for value in _reader(database, tmp_path).articles()["items"]] == ["good"]
    assert lifecycle.item_status(root, database, candidate_sha256s=[sha])["held"][0]["reason"] == "awaiting_actual_reply"
    from climate_registry.cli import main as registry_main
    review_path = tmp_path / "owner-reviews.json"
    review_path.write_text(json.dumps(receipts), encoding="utf-8")
    assert registry_main(["approve-owner-check", "--database", str(database), "--input", str(review_path),
        "--lifecycle-root", prepared["lifecycle_root"], "--queue-dir", str(queue), "--backup-dir", str(backups), "--apply"]) == 2
    assert "held" in json.loads(capsys.readouterr().out)["message"]
    assert database.read_bytes() == before and list(backups.iterdir()) == backup_files
    # A separate ready sibling can proceed; the exact held item needs its actual reply.
    assert approve_owner_check(database, receipts[1], lifecycle_root=tmp_path / "runs", queue_dir=queue, backup_dir=backups, apply=True)["status"] == "approved"
    lifecycle.import_decisions(root, database, [_reply(dispute, sha, identity="good")])
    assert approve_owner_check(database, receipts[0], lifecycle_root=tmp_path / "runs", queue_dir=queue, backup_dir=backups, apply=True)["status"] == "approved"
    assert _reader(database, tmp_path).article("good")["published_candidate_sha256"] == sha


@pytest.mark.parametrize("problem", ["no_root", "missing", "unreadable", "corrupt", "unsupported", "mismatched", "invalid", "missing_scope"])
def test_owner_approval_requires_actual_readable_exact_lifecycle_context(tmp_path, monkeypatch, problem):
    from climate_registry.owner_checks import approve_owner_check
    from test_verified_publication import _database as owner_database
    from test_owner_checks import _manifest, _review
    database = owner_database(tmp_path)
    root, queue, backups = tmp_path / "runs", tmp_path / "queue", tmp_path / "backups"
    prepared = prepare_owner_checks(database, [_manifest(database)], root=root)
    staged = ingest_owner_check(database, prepared["ready"], queue_dir=queue, backup_dir=backups, apply=True)
    receipt = _review(database, tmp_path, staged["results"][0]["candidate_sha256"])
    path = root / "item-lifecycle/state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    if problem == "missing":
        path.unlink()
    elif problem == "unreadable":
        original = Path.read_text
        def unreadable(current, *args, **kwargs):
            if current == path:
                raise PermissionError("lifecycle ledger is unreadable")
            return original(current, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", unreadable)
    elif problem == "corrupt":
        path.write_bytes(b"{")
    elif problem != "no_root":
        if problem == "unsupported":
            state["schema_version"] = "unsupported-version"
        elif problem == "mismatched":
            other = tmp_path / "other.sqlite3"
            other.write_bytes(database.read_bytes())
            state["registry_database"] = str(other)
        elif problem == "invalid":
            state["disputes"] = []
        else:
            state["items"] = {}
        path.write_text(json.dumps(state), encoding="utf-8")
    before, backups_before = database.read_bytes(), list(backups.iterdir())
    with pytest.raises(RegistryInputError):
        approve_owner_check(database, receipt, lifecycle_root=None if problem == "no_root" else root,
            queue_dir=queue, backup_dir=backups, apply=True)
    assert database.read_bytes() == before and list(backups.iterdir()) == backups_before


def test_dispute_writer_cannot_interleave_after_owner_gate_before_commit(tmp_path, monkeypatch):
    from climate_registry import owner_checks
    from climate_registry.errors import RegistryLockError
    from test_verified_publication import _database as owner_database
    from test_owner_checks import _manifest, _review
    database = owner_database(tmp_path)
    root, queue, backups = tmp_path / "runs", tmp_path / "queue", tmp_path / "backups"
    prepared = prepare_owner_checks(database, [_manifest(database)], root=root)
    staged = ingest_owner_check(database, prepared["ready"], queue_dir=queue, backup_dir=backups, apply=True)
    sha = staged["results"][0]["candidate_sha256"]
    receipt = _review(database, tmp_path, sha)
    ledger = (root / "item-lifecycle/state.json").read_bytes()
    original, attempts = owner_checks._approve_one, []
    def interleave(connection, selected, proof, apply):
        if apply:
            with pytest.raises(RegistryLockError, match="locked"):
                _hold(root, database, sha)
            attempts.append(sha)
        return original(connection, selected, proof, apply)
    monkeypatch.setattr(owner_checks, "_approve_one", interleave)
    assert owner_checks.approve_owner_check(database, receipt, lifecycle_root=root,
        queue_dir=queue, backup_dir=backups, apply=True)["status"] == "approved"
    assert attempts == [sha] and (root / "item-lifecycle/state.json").read_bytes() == ledger
    assert lifecycle._load(root, database)["disputes"] == {}


def test_owner_context_needs_exact_prepared_input_not_same_entity_history(tmp_path):
    from climate_registry.owner_checks import approve_owner_check, owner_review_template
    from test_verified_publication import _database as owner_database
    from test_owner_checks import _manifest
    database = owner_database(tmp_path)
    stale_root, root = tmp_path / "old-runs", tmp_path / "runs"
    old = _manifest(database)
    prepare_owner_checks(database, [old], root=stale_root)
    old_sha = old["plan"]["expected_state"]["latest_candidate_sha256"]
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE articles SET last_seen='2026-10-26' WHERE article_id='good'")
    manifest = _manifest(database)
    input_sha = manifest["plan"]["expected_state"]["latest_candidate_sha256"]
    assert input_sha != old_sha
    prepared = prepare_owner_checks(database, [manifest], root=root)
    staged = ingest_owner_check(database, prepared["ready"], queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups", apply=True)
    sha = staged["results"][0]["candidate_sha256"]
    assert sha not in {old_sha, input_sha}
    receipt = owner_review_template(database, sha)
    receipt.update(actor={"id": "independent-reviewer", "task_id": "separate-task"},
        authorization_reference="owner-message-actual-scope", audit_reference="audit/exact-input",
        reviewed_at="2026-10-27T00:00:00Z", decision="pass", reason="Inspected exact capture and selected fields")
    retained = lambda context: next(iter(lifecycle._load(context, database)["items"].values()))["candidates"]
    assert set(retained(stale_root)) == {old_sha} and set(retained(root)) == {input_sha}
    before = database.read_bytes()
    with pytest.raises(RegistryInputError, match="exact owner candidate/input scope"):
        approve_owner_check(database, receipt, lifecycle_root=stale_root,
            queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups", apply=True)
    assert database.read_bytes() == before
    assert approve_owner_check(database, receipt, lifecycle_root=root,
        queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups", apply=True)["status"] == "approved"
    assert set(retained(root)) == {input_sha}  # Approval did not register its new SHA or rewrite the ledger.


def test_owner_meeting_approval_rechecks_unselected_required_article_and_keeps_same_body_exception(tmp_path, monkeypatch):
    from climate_registry.owner_checks import approve_owner_check
    from test_issue197_workflow import _native_meeting_material_fixture
    from test_owner_checks import _manifest, _review
    database, _, process, _ = _native_meeting_material_fixture(tmp_path, monkeypatch)
    process("first")
    _complete_fixture_information(database)
    root, queue = tmp_path / "runs", tmp_path / "queue"
    with sqlite3.connect(database) as connection:
        article, meeting, source = connection.execute("SELECT article_id,event_id,event_source_id FROM climate_event_sources LIMIT 1").fetchone()
    fields = {"name": "World Climate Summit 2027", "event_type": "conference", "status": "scheduled",
        "raw_date": "10–12 June 2027", "start_date": "2027-06-10", "end_date": "2027-06-12", "date_precision": "day"}
    manifest = _manifest(database, kind="meeting", identity=meeting, occurrence=source, fields=fields,
        body="World Climate Summit 2027\n10–12 June 2027\nThe conference is scheduled.")
    manifest["checked_at"] = "2026-10-26T23:00:00Z"
    manifest["observations"][0]["capture"]["captured_at"] = "2026-10-26T22:59:00Z"
    for field in ("event_type", "status"):
        manifest["observations"][0]["field_evidence"][field]["excerpt"] = "The conference is scheduled."
    prepared = prepare_owner_checks(database, [manifest], root=root)
    staged = ingest_owner_check(database, prepared["ready"], queue_dir=queue, backup_dir=tmp_path / "backups", apply=True)
    sha = staged["results"][0]["candidate_sha256"]
    article_sha = _candidates(database)[article]
    article_dispute = _hold(root, database, article_sha)
    with sqlite3.connect(database) as connection:
        assert lifecycle.partition_items(connection, root, [sha], require_information=False)["held"][0]["stage"] == "dependency_held"
    receipt = _review(database, tmp_path, sha)
    receipt["reviewed_at"] = "2026-10-27T00:00:00Z"
    before = database.read_bytes()
    with pytest.raises(RegistryInputError, match="held|dispute"):
        approve_owner_check(database, receipt, lifecycle_root=tmp_path / "runs", queue_dir=queue, backup_dir=tmp_path / "backups", apply=True)
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='meeting' AND entity_id=?", (meeting,)).fetchone()[0] is None
    lifecycle.import_decisions(root, database, [_reply(article_dispute, article_sha, identity=article)])
    with sqlite3.connect(database) as connection:
        # An explicit isolated fixture approval establishes the identical approved body.
        _approve(connection, article_sha, {"basis": "isolated approved same-content baseline"})
        connection.execute("UPDATE articles SET last_seen='2026-10-26' WHERE article_id=?", (article,))
        stage_entities(connection, entities={("article", article)})
    current = _candidates(database)[article]
    assert current != article_sha
    _hold(root, database, current)
    exact_public_hold = lifecycle.record_dispute(root, database, article_sha, text="A new exact public-body dispute",
        evidence=["a later business report"], options=["investigate", "keep"], impact="Hold new Dates approval, retain old Article public")
    with pytest.raises(RegistryInputError, match="held"):
        approve_owner_check(database, receipt, lifecycle_root=root, queue_dir=queue, backup_dir=tmp_path / "backups", apply=True)
    with sqlite3.connect(database) as connection:
        held, = lifecycle.partition_items(connection, root, [sha], require_information=False)["held"]
    assert held["dependency"]["candidate_sha256"] == article_sha
    assert held["dependency"]["dispute_id"] == exact_public_hold["dispute_id"]
    lifecycle.import_decisions(root, database, [_reply(exact_public_hold, article_sha, identity=article, decision_id="reply-public-body")])
    assert approve_owner_check(database, receipt, lifecycle_root=tmp_path / "runs", queue_dir=queue, backup_dir=tmp_path / "backups", apply=True)["status"] == "approved"


@pytest.mark.parametrize("check_undisputed", [False, True])
def test_g1_date_publication_does_not_release_dates_from_disputed_new_article_body(tmp_path, monkeypatch, check_undisputed):
    import hashlib
    from climate_registry.enrichment_contract import CONTENT_SCOPE
    from climate_monitor.meetings import process_batch
    from climate_registry.acquisition_review import claim_review
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    from climate_registry.owner_checks import plan_owner_check, owner_review_template, approve_owner_check
    from climate_registry.publication import snapshot_entity, review_candidates
    from climate_registry.read_api import RegistryReader
    from test_issue112_acquisition import _database as acquisition_database
    from test_issue136_meetings import _stored_batch, _stored_item, _candidate
    from test_issue197_workflow import _native, _native_read
    from test_owner_checks import _manifest
    database = acquisition_database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    root, queue, backups = tmp_path / "runs", tmp_path / "queue", tmp_path / "backups"
    url = "https://example.com/versioned-g8"
    body_a = ("World Climate Summit 2027 meets June 10–12, 2027 in New York. Registration deadline May 1, 2027. "
        "Register https://example.com/register. Climate risk agenda. The conference is scheduled.")
    batches = []
    for label, observed, body in (("a", "2026-09-10T08:00:00Z", body_a),
            ("b", "2026-10-01T08:00:00Z", body_a + "\nA substantive new source body about flood risk.")):
        payload = _stored_batch("body-" + label, observed[:10], observed, _stored_item(url, body, observed))
        store_acquisition_batch(database, payload)
        retained = load_acquisition_batch(database, payload["batch_id"])["items"][0]
        # Select the real fetched body as live fixture input; only real writers publish it.
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE articles SET current_content_version_id=? WHERE article_id=?", (retained["content_version_id"], retained["article_id"]))
        assert process_batch(database, payload["batch_id"], prompt_text="extract exact " + label, prompt_version=label,
            provider="test", model="test", extractor=lambda request: {"events": [_candidate()]})["status"] == "succeeded"
        batches.append(payload)
        if label == "a":
            _approve_native_packet(database, root / "registry-review", tmp_path / "a-native.db", "cron_initial_body_a")
    _complete_fixture_information(database)
    item = load_acquisition_batch(database, batches[1]["batch_id"])["items"][0]
    article, body_b = item["article_id"], item["content_version_id"]
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    def approved_body():
        with sqlite3.connect(database) as connection:
            sha = connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id=?", (article,)).fetchone()[0]
            return next(row["current_content_version_id"] for row in lifecycle._snapshot(connection, sha)["tables"]["articles"] if row["article_id"] == article)
    body_a_id = approved_body()
    assert body_a_id != body_b
    summary = "World Climate Summit 2027 is scheduled for June 10–12, 2027 in New York; the source adds flood-risk information."
    with sqlite3.connect(database) as connection:
        snapshot = snapshot_entity(connection, "article", article)
        old_body = next(row for row in snapshot["tables"]["article_content_versions"] if row["content_version_id"] == body_a_id)
        snapshot["derived_display"] = {"summary": summary, "content_update": {
            "schema_version": "article-content-update.v1", "content_version_id": body_b, "body_sha256": item["content_sha256"],
            "previous_content_version_id": body_a_id, "previous_body_sha256": old_body["content_sha256"],
            "summary": summary, "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(),
            "summary_content_version_id": body_b, "summary_support": "complete",
            "change_note": "The source added flood-risk information while retaining the conference details.",
            "change_evidence": {"basis": "body_comparison", "previous_excerpt": body_a,
                "current_excerpt": "A substantive new source body about flood risk."},
            "first_observed_at": batches[1]["started_at"], "date_evidence": {}}}
        stage_snapshot(connection, snapshot, enrichment_scope=CONTENT_SCOPE)
    with sqlite3.connect(database) as connection:
        event, source_b = connection.execute("SELECT event_id,event_source_id FROM climate_event_sources WHERE article_id=? AND content_version_id=?", (article, body_b)).fetchone()
        assert {row[0] for row in connection.execute("SELECT content_version_id FROM climate_event_sources WHERE event_id=?", (event,))} == {body_a_id, body_b}
    fields = {"name": "World Climate Summit 2027", "event_type": "conference", "status": "scheduled",
        "raw_date": "June 10–12, 2027", "start_date": "2027-06-10", "end_date": "2027-06-12", "date_precision": "day"}
    manifest = _manifest(database, kind="meeting", identity=event, occurrence=source_b, fields=fields, body=body_a)
    manifest["checked_at"] = "2026-10-26T23:00:00Z"
    manifest["observations"][0]["capture"]["captured_at"] = "2026-10-26T22:59:00Z"
    for field in ("event_type", "status"):
        manifest["observations"][0]["field_evidence"][field]["excerpt"] = "The conference is scheduled."
    for field in ("start_date", "end_date", "date_precision"):
        manifest["observations"][0]["field_evidence"][field]["excerpt"] = "June 10–12, 2027"
    prepared = prepare_owner_checks(database, [manifest], root=root)
    # Retain a real private in-flight owner receipt, even when current preparation holds it.
    staged = ingest_owner_check(database, [manifest], queue_dir=queue, backup_dir=backups, apply=True)
    meeting_sha = staged["results"][0]["candidate_sha256"]
    b_sha = _candidates(database)[article]
    with sqlite3.connect(database) as connection:
        frozen_b = lifecycle._snapshot(connection, b_sha)
        assert frozen_b["enrichment_scope"]["scope"] == CONTENT_SCOPE
    lifecycle.record_items(root, database, [meeting_sha])
    receipt = owner_review_template(database, meeting_sha)
    receipt.update(actor={"id": "independent-reviewer", "task_id": "review-active-b"},
        authorization_reference="owner-message:actual-scope", audit_reference="audit/active-b",
        reviewed_at="2026-10-27T00:00:00Z", decision="pass", reason="Inspected exact active B source and selected Dates fields")
    def approve_meeting():
        return approve_owner_check(database, receipt, lifecycle_root=root, queue_dir=queue, backup_dir=backups, apply=True)
    if check_undisputed:
        with pytest.raises(RegistryInputError, match="held"):
            approve_meeting()
        assert prepared["ready"] == []
        packet = prepare_review(database, root / "registry-review")
        assert meeting_sha not in {row["candidate_sha256"] for row in packet["candidates"]}
        assert any(row["entity_id"] == article for row in packet["candidates"])
        with sqlite3.connect(database) as connection:
            with pytest.raises(ValueError, match="held"):
                lifecycle.require_no_disputes(connection, root, {"candidates": [{"candidate_sha256": meeting_sha}]})
    dispute = _hold(root, database, b_sha)
    with pytest.raises(RegistryInputError, match="held"):
        approve_meeting()
    date_manifest = plan_owner_check(database, "article", article, date_fields=["publication_date"], human_date_override=True)
    instruction = "I confirmed this record: set publication_date to 2026-10-02."
    date_manifest.update(actor={"id": "operator", "task_id": "date-instruction"}, checked_at="2026-10-25T12:00:00Z",
        authorization_reference="human-message:date", audit_reference="audit/date",
        manual_authorization={"instruction": instruction, "authorized_by": "owner", "scope": "record"},
        observations=[{"target": date_manifest["plan"]["targets"][0], "public_fields": {"publication_date": "2026-10-02"},
            "field_evidence": {"publication_date": {"text": "2026-10-02", "precision": "day"}}}])
    lifecycle.import_decisions(root, database, [{"decision_id": "date-only", "kind": "human_date", "actor": "owner",
        "reference": "human-message:date", "received_at": "2026-10-25T12:00:00Z", "text": instruction,
        "target": {"entity_kind": "article", "entity_id": article}, "manifest": date_manifest}])
    assert lifecycle.resume_items(root, database, decision_id="date-only", queue_dir=queue, backup_dir=backups)["status"] == "executed"
    assert approved_body() == body_a_id
    assert reader.article(article)["publication_date"] == "2026-10-02"
    # A retained PASS/rollback receipt without a committed pointer is not approval history.
    standalone = {"basis": "display rollback receipt; no Registry publication occurred"}
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO registry_reviews VALUES(?,?,?,?,?)",
            (digest([b_sha, "pass", standalone]), b_sha, "pass", "2026-10-26T00:00:00Z", json.dumps(standalone)))
        assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id=?", (article,)).fetchone()[0] != b_sha
    public_before = reader.meetings(base_date="2026-10-25")
    try:
        result = approve_meeting()
    except RegistryInputError as exc:
        assert "held" in str(exc)
    else:
        pytest.fail(json.dumps({"unexpected": result["status"], "required_body": body_b,
            "published_article_body": approved_body(),
            "public_meeting": reader.meetings(base_date="2026-10-25"), "business_replies": 0}))
    assert reader.meetings(base_date="2026-10-25") == public_before
    assert not any(source["content_version_id"] == body_b for row in public_before["items"] for source in row.get("sources", []))
    with sqlite3.connect(database) as connection:
        held, = lifecycle.partition_items(connection, root, [meeting_sha], require_information=False)["held"]
    assert held["dependency"]["content_version_id"] == body_b and held["stage"] == "dependency_held"
    assert not any(row["candidate_sha256"] == meeting_sha for row in lifecycle.item_status(root, database)["ready"])
    current_manifest = _manifest(database, kind="meeting", identity=event, occurrence=source_b, fields=fields, body=body_a)
    current_manifest["observations"] = manifest["observations"]
    current_manifest["checked_at"] = manifest["checked_at"]
    assert prepare_owner_checks(database, [current_manifest], root=root)["ready"] == []
    lifecycle.import_decisions(root, database, [_reply(dispute, b_sha, identity=article, decision_id="reply-original-b")])
    from verified_fixtures import seed_verified_articles
    with sqlite3.connect(database) as connection:
        seed_verified_articles(connection, occurrence_ids={"core:" + article})
        rebased = snapshot_entity(connection, "article", article)
        rebased["derived_display"] = deepcopy(frozen_b["derived_display"])
        stage_snapshot(connection, rebased, enrichment_scope=CONTENT_SCOPE)
    current_b = _candidates(database)[article]
    if current_b != b_sha:
        current_dispute = _hold(root, database, current_b)
        lifecycle.import_decisions(root, database, [_reply(current_dispute, current_b, identity=article, decision_id="reply-reconciled-b")])
    with sqlite3.connect(database) as connection:
        resumed = lifecycle.partition_items(connection, root, [current_b, meeting_sha], require_information=False)
    assert [row["candidate_sha256"] for row in resumed["ready"]] == [current_b]
    assert resumed["held"][0]["candidate_sha256"] == meeting_sha
    assert resumed["held"][0]["next_owner"] == "G10"
    before_reply_approval = database.read_bytes()
    with pytest.raises(RegistryInputError, match="held"):
        approve_meeting()
    assert database.read_bytes() == before_reply_approval
    review_root = root / "registry-review"
    packet = prepare_review(database, review_root)
    assert meeting_sha not in {row["candidate_sha256"] for row in packet["candidates"]}
    candidate, = [row for row in packet["candidates"] if row["entity_id"] == article]
    native, session = tmp_path / "b-native.db", "cron_approve_real_body_b"
    _native(native, session)
    for row in packet["candidates"]:
        path = Path(row["snapshot_path"])
        _native_read(native, session, path, path.read_text(encoding="utf-8"), row["candidate_sha256"])
    claim = claim_review(review_root, packet, session_id=session, execution_id="b", reviewer="native", hermes_database=native)
    result = review_candidates(review_root, claim["token"], {candidate["candidate_sha256"]: {
        "candidate_sha256": candidate["candidate_sha256"], "enrichment_contract_sha256": candidate["enrichment_contract_sha256"],
        "status": "pass", "reason": "Inspected exact complete B content update, source checks, summary and inherited date authority"}})
    assert result["reviewed"] == 1
    assert approved_body() == body_b
    old_article_public = reader.article(article)
    published_dispute = lifecycle.record_dispute(root, database, current_b,
        text="Check the newly reported identity conflict on this published body",
        evidence=["actual later business report for exact B"], options=["investigate", "keep"],
        impact="Keep the old Article public but hold dependent Dates pending an actual reply")
    with pytest.raises(RegistryInputError, match="held"):
        approve_meeting()
    assert reader.article(article) == old_article_public
    with sqlite3.connect(database) as connection:
        held, = lifecycle.partition_items(connection, root, [meeting_sha], require_information=False)["held"]
    assert held["next_owner"] == "owner" and held["dependency"]["candidate_sha256"] == current_b
    lifecycle.import_decisions(root, database, [_reply(published_dispute, current_b, identity=article, decision_id="reply-published-b")])
    historic_a = date_manifest["plan"]["baseline_candidate_sha256"]
    historic_dispute = _hold(root, database, historic_a)
    with pytest.raises(RegistryInputError, match="held"):
        approve_meeting()
    lifecycle.import_decisions(root, database, [_reply(historic_dispute, historic_a, identity=article, decision_id="reply-historic-a")])
    assert approve_meeting()["status"] == "approved"
    assert any(source["content_version_id"] == body_b and source["is_current"] for row in reader.meetings(base_date="2026-10-25")["items"] for source in row.get("sources", []))


def test_dates_with_no_candidate_or_published_required_body_stays_held(tmp_path, monkeypatch):
    from test_issue197_workflow import _native_meeting_material_fixture
    database, _, process, _ = _native_meeting_material_fixture(tmp_path, monkeypatch)
    process("first")
    _complete_fixture_information(database)
    root = tmp_path / "runs"
    with sqlite3.connect(database) as connection:
        article, meeting, version = connection.execute("SELECT article_id,event_id,content_version_id FROM climate_event_sources LIMIT 1").fetchone()
        meeting_sha = _candidates(database)[meeting]
        # The source relationship is real, but no current/retained/approved Article selects its body.
        connection.execute("UPDATE articles SET current_content_version_id=NULL WHERE article_id=?", (article,))
        stage_entities(connection, entities={("article", article)})
    article_sha = _candidates(database)[article]
    with sqlite3.connect(database) as connection:
        partition = lifecycle.partition_items(connection, root, [meeting_sha, article_sha], require_information=False)
        held, = partition["held"]
        assert held["candidate_sha256"] == meeting_sha
        assert held["dependency"]["content_version_id"] == version
        assert held["dependency"]["candidate_sha256"] is None
        assert [row["candidate_sha256"] for row in partition["ready"]] == [article_sha]
        packet = {"candidates": [{"candidate_sha256": meeting_sha}]}
        for gate in (lifecycle.require_item_readiness, lifecycle.require_no_disputes):
            with pytest.raises(ValueError, match="held"):
                gate(connection, root, packet)
    prepared = prepare_review(database, root / "registry-review")
    assert meeting_sha not in {row["candidate_sha256"] for row in prepared["candidates"]}
    preparation = json.loads((root / "registry-review" / "preparation.json").read_text())
    assert any(row["entity_id"] == meeting and row["stage"] == "dependency_held" for row in preparation["held"])


def test_meeting_only_preparation_resolves_unselected_article_business_dependency(tmp_path, monkeypatch):
    from climate_registry.owner_checks import approve_owner_check
    from test_issue197_workflow import _native_meeting_material_fixture
    from test_owner_checks import _manifest
    database, _, process, _ = _native_meeting_material_fixture(tmp_path, monkeypatch)
    process("first")
    _complete_fixture_information(database)
    root = tmp_path / "runs"
    with sqlite3.connect(database) as connection:
        article, meeting, source = connection.execute("SELECT article_id,event_id,event_source_id FROM climate_event_sources LIMIT 1").fetchone()
    fields = {"name": "World Climate Summit 2027", "event_type": "conference", "status": "scheduled",
        "raw_date": "10–12 June 2027", "start_date": "2027-06-10", "end_date": "2027-06-12", "date_precision": "day"}
    manifest = _manifest(database, kind="meeting", identity=meeting, occurrence=source, fields=fields,
        body="World Climate Summit 2027\n10–12 June 2027\nThe conference is scheduled.\nPRIVATE TOOL FOOTER")
    for field in ("event_type", "status"):
        manifest["observations"][0]["field_evidence"][field]["excerpt"] = "The conference is scheduled."
    shas = _candidates(database)
    dispute = _hold(root, database, shas[article])
    packet = {"schema_version": "registry-final-review.v1", "candidates": [{"candidate_sha256": shas[meeting]}]}
    before = database.read_bytes()
    prepared = prepare_owner_checks(database, [manifest], root=root)
    assert prepared["ready"] == [] and prepared["held"][0]["stage"] == "dependency_held"
    assert prepared["held"][0]["dependency"]["candidate_sha256"] == shas[article]
    for operation in (ingest_owner_check, approve_owner_check):
        with pytest.raises(RegistryInputError, match="1 to 100"):
            operation(database, [], queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups", apply=True)
    with sqlite3.connect(database) as connection:
        for require in (lifecycle.require_item_readiness, lifecycle.require_no_disputes):
            with pytest.raises(ValueError, match="held|dispute"):
                require(connection, root, packet)
        assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='meeting' AND entity_id=?", (meeting,)).fetchone()[0] is None
    assert database.read_bytes() == before
    lifecycle.import_decisions(root, database, [_reply(dispute, shas[article], identity=article)])
    assert prepare_owner_checks(database, [manifest], root=root)["ready"] == []
    # A later held Article candidate does not withdraw an approved identical source body.
    with sqlite3.connect(database) as connection:
        _approve(connection, shas[article], {"basis": "explicit isolated same-content Article approval"})
        connection.execute("UPDATE articles SET last_seen='2026-10-26' WHERE article_id=?", (article,))
        stage_entities(connection)
    current = _candidates(database)[article]
    assert current != shas[article]
    _hold(root, database, current)
    assert prepare_owner_checks(database, [manifest], root=root)["ready"] == [manifest]
    with sqlite3.connect(database) as connection:
        lifecycle.require_item_readiness(connection, root, packet)
        lifecycle.require_no_disputes(connection, root, packet)


@pytest.mark.parametrize("malformed_plan", [[], None])
def test_owner_preparation_isolates_nonobject_plan(tmp_path, malformed_plan):
    from test_verified_publication import _database as owner_database
    from test_owner_checks import _manifest
    database = owner_database(tmp_path)
    valid = _manifest(database)
    before = database.read_bytes()
    result = prepare_owner_checks(database, [valid, {"plan": malformed_plan}], root=tmp_path / "runs")
    assert result["ready"] == [valid] and result["held"] == []
    assert len(result["invalid"]) == 1 and result["invalid"][0]["index"] == 1
    assert result["invalid"][0]["entity_kind"] is None and result["invalid"][0]["entity_id"] is None
    assert database.read_bytes() == before


@pytest.mark.parametrize("shape", ["typed_null_plan", "typed_list_plan", "ordinary_null_checked_at"])
@pytest.mark.parametrize("bad_index", [0, 1])
def test_owner_preparation_isolates_typed_plan_and_null_timestamp(tmp_path, shape, bad_index):
    from climate_registry.owner_dates import DATE_CHECK_VERSION
    from test_verified_publication import _database as owner_database
    from test_owner_checks import _manifest
    database = owner_database(tmp_path)
    good = _manifest(database)
    if shape == "ordinary_null_checked_at":
        bad = _manifest(database, identity="unchecked", occurrence="core:unchecked")
        bad["checked_at"] = None
    else:
        bad = {"schema_version": DATE_CHECK_VERSION, "plan": None if shape == "typed_null_plan" else []}
    before = database.read_bytes()
    good_only = prepare_owner_checks(database, [good], root=tmp_path / "runs")
    assert good_only["ready"] == [good] and good_only["held"] == [] and good_only["invalid"] == []
    assert database.read_bytes() == before
    batch = [good]
    batch.insert(bad_index, bad)
    result = prepare_owner_checks(database, batch, root=tmp_path / "runs")
    assert result["ready"] == [good] and result["held"] == [] and result["execution"] == "not_dispatched"
    invalid, = result["invalid"]
    assert invalid["index"] == bad_index and invalid["classification"] == "invalid"
    assert ("checked_at" if shape == "ordinary_null_checked_at" else "plan") in invalid["reason"]
    assert (invalid["entity_kind"], invalid["entity_id"]) == (("article", "unchecked") if shape == "ordinary_null_checked_at" else (None, None))
    assert database.read_bytes() == before


@pytest.mark.parametrize("bad_index", [0, 1])
def test_owner_preparation_isolates_null_date_field_evidence(tmp_path, bad_index):
    from test_owner_publication_dates import _seed, _date_fields_manifest
    from test_owner_checks import _manifest
    database = _seed(tmp_path)
    good = _manifest(database, identity="unchecked", occurrence="core:unchecked")
    bad = _date_fields_manifest(database)
    bad["observations"][0]["field_evidence"] = None
    before = database.read_bytes()
    good_only = prepare_owner_checks(database, [good], root=tmp_path / "runs")
    assert good_only["ready"] == [good] and good_only["held"] == [] and good_only["invalid"] == []
    assert database.read_bytes() == before
    batch = [good]
    batch.insert(bad_index, bad)
    result = prepare_owner_checks(database, batch, root=tmp_path / "runs")
    assert result["ready"] == [good] and result["held"] == [] and result["execution"] == "not_dispatched"
    invalid, = result["invalid"]
    assert invalid["index"] == bad_index and invalid["classification"] == "invalid"
    assert (invalid["entity_kind"], invalid["entity_id"]) == ("article", "good")
    assert "field_evidence" in invalid["reason"]
    assert database.read_bytes() == before


@pytest.mark.parametrize("bad_index", [0, 1])
@pytest.mark.parametrize("malformed", [None, [], {}, "", 0, False], ids=["null", "list", "object", "string", "number", "boolean"])
@pytest.mark.parametrize("path", [
    ("plan",), ("plan", "targets"), ("plan", "expected_state"),
    ("plan", "entity_kind"), ("plan", "entity_id"), ("actor",), ("checked_at",),
    ("observations",), ("observations", 0), ("observations", 0, "target"),
    ("observations", 0, "target", "fields"), ("observations", 0, "capture"),
    ("observations", 0, "capture", "captured_at"), ("observations", 0, "public_fields"),
    ("observations", 0, "field_evidence"), ("observations", 0, "field_evidence", "summary"),
    ("observations", 0, "original_comparisons"), ("observations", 0, "original_comparisons", "summary"),
], ids=lambda path: ".".join(map(str, path)))
def test_owner_preparation_isolates_nested_json_shapes(tmp_path, bad_index, malformed, path):
    from test_verified_publication import _database as owner_database
    from test_owner_checks import _manifest
    database = owner_database(tmp_path)
    good = _manifest(database)
    bad = json.loads(json.dumps(_manifest(database, identity="unchecked", occurrence="core:unchecked")))
    parent = bad
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = malformed
    if path[:3] == ("observations", 0, "target"):
        bad["plan"]["targets"][0] = bad["observations"][0]["target"]
    before = database.read_bytes()
    good_only = prepare_owner_checks(database, [good], root=tmp_path / "runs")
    assert good_only["ready"] == [good] and good_only["held"] == [] and good_only["invalid"] == []
    assert database.read_bytes() == before
    batch = [good]
    batch.insert(bad_index, bad)
    result = prepare_owner_checks(database, batch, root=tmp_path / "runs")
    assert result["ready"] == [good] and result["held"] == [] and result["execution"] == "not_dispatched"
    invalid, = result["invalid"]
    assert invalid["index"] == bad_index and invalid["classification"] == "invalid" and invalid["reason"]
    assert all(value is None or isinstance(value, str) for value in (invalid["entity_kind"], invalid["entity_id"]))
    if path == ("observations", 0, "target", "fields") and not isinstance(malformed, dict):
        assert "target fields" in invalid["reason"]
    assert database.read_bytes() == before


@pytest.mark.parametrize("stage_before_import", [True, False])
def test_unregistered_pending_content_survives_date_result_loss_and_replay(tmp_path, monkeypatch, stage_before_import):
    from test_enrichment_contract import _published_a, _stage_update
    from test_verified_publication import _reader
    database, _ = _published_a(tmp_path)
    root = tmp_path / "runs"
    if stage_before_import:
        b, _ = _stage_update(database)
    lifecycle.import_decisions(root, database, [_date_reply(database)])
    if not stage_before_import:
        b, _ = _stage_update(database)
    assert lifecycle._load(root, database)["items"] == {}
    save = lifecycle._save
    def lose_result(root, state):
        if state["decisions"]["date-1"].get("result"):
            raise OSError("date result write lost")
        save(root, state)
    monkeypatch.setattr(lifecycle, "_save", lose_result)
    with pytest.raises(OSError, match="result write lost"):
        lifecycle.resume_items(root, database, decision_id="date-1", queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups")
    monkeypatch.setattr(lifecycle, "_save", save)
    retained = lifecycle._load(root, database)
    assert any(b in item["candidates"] for item in retained["items"].values())
    assert retained["decisions"]["date-1"]["execution_manifests"][-1]["plan"]["expected_state"]["latest_candidate_sha256"] == b
    before = database.read_bytes()
    assert lifecycle.resume_items(root, database, decision_id="date-1", queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups")["status"] == "recovered"
    assert database.read_bytes() == before
    status = lifecycle.resume_items(root, database)["items"]
    assert any(item["candidate_sha256"] == b for item in status["invalid"])
    assert "20 billion" not in _reader(database, tmp_path).article("good")["summary"]
    saved = (root / "item-lifecycle/state.json").read_bytes()
    assert lifecycle.resume_items(root, database, decision_id="date-1")["status"] == "no-op"
    assert (root / "item-lifecycle/state.json").read_bytes() == saved
