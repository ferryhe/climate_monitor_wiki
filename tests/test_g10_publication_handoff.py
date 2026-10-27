"""Isolated G10 interface fixtures; no production agent, review or publication runs."""
import sqlite3
from pathlib import Path

import pytest

from climate_registry.publication import public_revision, public_wiki_snapshot, review_activation_status
from test_article_content_dates import _manual, ordered_source_check_fixture_clock
from test_enrichment_contract import _assert_native_content_api, _native_contract_review, _published_a, _stage_update
from test_owner_checks import _ingest
from test_registry_publication import _actual_chat_activation, _approve_native_packet, _database
from test_verified_publication import _reader


def test_native_t10_prompt_covers_exact_complete_contract_and_handoff():
    prompt = (Path(__file__).resolve().parents[1] / "monitoring/acquisition-review.md").read_text(encoding="utf-8")
    for requirement in ("enrichment_contract", "enrichment_contract_sha256", "content_comparison_baseline",
            "approval_inheritance_baseline", "publication_date_only", "approved_candidates", "public_revision",
            "downstream_sync_status=not_checked"):
        assert requirement in prompt


def test_g10_full_update_native_fixture_handoff_and_downstream_only_retry(tmp_path, monkeypatch):
    from climate_registry.pdf_pipeline import PdfIntakePipeline
    import climate_registry.pdf_pipeline as pipeline_module

    database, _ = _published_a(tmp_path)
    _ingest(database, tmp_path, _manual(database))
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    application = tmp_path / "application"; application.mkdir()
    queue = tmp_path / "g10-queue"; queue.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    responder, reload = _actual_chat_activation(monkeypatch, database, application, queue, runtime)
    writer = PdfIntakePipeline(queue, database, tmp_path / "backups", runtime, reload, repository_root=application)
    assert writer.process_next()["chat_ready"] is True
    old_revision = responder.kb.public_registry_revision
    before = _reader(database, tmp_path).article("good")
    # G7 author output is an isolated complete-package fixture. The native read/session
    # adapter below exercises separate source-checker and reviewer identities.
    candidate, frozen = _stage_update(database)
    assert _reader(database, tmp_path).article("good") == before
    assert writer.process_next() is None
    assert public_revision(database) == old_revision

    reviews = _native_contract_review(database, tmp_path, monkeypatch)
    handoff = review_activation_status(tmp_path / "native-review")
    assert handoff["status"] == "published" and handoff["downstream_sync_status"] == "not_checked"
    approved = next(item for item in handoff["approved_candidates"] if item["entity_id"] == "good")
    assert approved["candidate_sha256"] == candidate and approved["review_id"] in reviews["review_ids"]
    assert handoff["public_revision"] == public_revision(database) != old_revision
    reader = _reader(database, tmp_path)
    actual = reader.article("good")
    assert actual["publication_date"] == before["publication_date"] == "2026-10-02"
    assert actual["content_date"] == actual["updated_date"] == "2026-10-16"
    assert actual["summary"] == frozen["derived_display"]["summary"]
    assert actual["content_update"]["change_note"] == frozen["derived_display"]["content_update"]["change_note"]
    _assert_native_content_api(reader, monkeypatch, candidate, actual["summary"])
    pages, wiki_revision = public_wiki_snapshot(database)
    assert wiki_revision == handoff["public_revision"]
    assert actual["summary"] in pages["article-good.md"]
    assert actual["content_update"]["change_note"] in pages["article-good.md"]
    assert "Updated:" in pages["article-good.md"]

    with sqlite3.connect(database) as db:
        review_rows = db.execute("SELECT * FROM registry_reviews ORDER BY review_id").fetchall()
        fetched = db.execute("SELECT * FROM article_content_versions ORDER BY content_version_id").fetchall()
    clock = ["2026-10-26T12:00:00+00:00"]
    monkeypatch.setattr(pipeline_module, "_now", lambda: clock[0])
    def unavailable(generation):
        raise TimeoutError("isolated reload fixture unavailable")
    writer.reload_chat = unavailable
    failed = writer.process_next()
    assert failed["stage"] == "failed" and failed["chat_ready"] is False
    assert failed["public_registry_revision"] == handoff["public_revision"]
    assert responder.kb.public_registry_revision == old_revision
    assert review_activation_status(tmp_path / "native-review") == handoff
    writer.reload_chat = reload
    clock[0] = "2026-10-26T12:01:00+00:00"
    recovered = writer.process_next()
    assert recovered["chat_ready"] is True
    assert recovered["public_registry_revision"] == responder.kb.public_registry_revision == handoff["public_revision"]
    assert recovered["generation_id"] == failed["prepared_generation"]["generation_id"]
    assert writer.process_next() is None
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM registry_reviews ORDER BY review_id").fetchall() == review_rows
        assert db.execute("SELECT * FROM article_content_versions ORDER BY content_version_id").fetchall() == fetched
    assert reader.article("good") == actual


def test_g10_display_failure_rolls_back_approval_and_has_no_successful_handoff(tmp_path, monkeypatch):
    import climate_registry.display as display
    database, _ = _published_a(tmp_path)
    candidate, _ = _stage_update(database)
    revision = public_revision(database)
    before = _reader(database, tmp_path).article("good")
    refresh = display.refresh_display
    def fail_after_approval(connection):
        if connection.execute("SELECT 1 FROM registry_publication WHERE published_candidate_sha256=?", (candidate,)).fetchone():
            raise OSError("isolated display refresh failure")
        return refresh(connection)
    monkeypatch.setattr(display, "refresh_display", fail_after_approval)
    with pytest.raises(OSError, match="isolated display refresh failure"):
        _native_contract_review(database, tmp_path, monkeypatch)
    handoff = review_activation_status(tmp_path / "native-review")
    assert handoff["approved_candidates"] == [] and handoff["approved_count"] == 0
    assert handoff["public_revision"] == revision
    assert _reader(database, tmp_path).article("good") == before
    with sqlite3.connect(database) as db:
        assert not db.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=? AND status='pass'", (candidate,)).fetchone()


def test_g10_handoff_revision_and_approvals_use_one_read_snapshot(tmp_path, monkeypatch):
    import climate_registry.publication as publication
    database = _database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    root = tmp_path / "runs/registry-review"
    _approve_native_packet(database, root, tmp_path / "hermes.sqlite3", "cron_handoff_snapshot")
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA journal_mode=WAL")
    revision = public_revision(database)
    original = publication.public_revision
    def change_publication_after_revision(database, *, connection=None):
        assert connection is not None and connection.in_transaction
        selected = original(database, connection=connection)
        publication.set_visibility(database, "article", "a", False)
        return selected
    monkeypatch.setattr(publication, "public_revision", change_publication_after_revision)
    handoff = review_activation_status(root)
    assert handoff["public_revision"] == revision != original(database)
    assert handoff["published_count"] == 2
    assert all(item["is_visible"] for item in handoff["approved_candidates"])
