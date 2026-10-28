"""Retained owner scope may expose its reviewed PDF, never an unchecked native core."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import sqlite3

import pytest

from climate_registry.acquisition_review import digest
from climate_registry.publication import published_snapshots
from climate_registry.verification import evaluate_snapshot, permitted_view
from climate_registry.read_api import RegistryNotFoundError
from test_verified_publication import _database, _reader


CASES = json.loads(gzip.decompress((Path(__file__).parent / "fixtures/legacy_pdf_owner_scopes.json.gz").read_bytes()))["items"]
APPROVED = next(item for item in CASES if item["candidate"]["entity_id"] == "article-03c3752c72830e638093f6c3")
HOLDS = [item for item in CASES if item is not APPROVED]


def _review(item):
    sha, receipt = item["candidate"]["candidate_sha256"], item["receipt"]
    return {"review_id": digest([sha, "pass", receipt]), "candidate_sha256": sha, "status": "pass",
        "reviewed_at": receipt["reviewed_at"], "receipt_json": json.dumps(receipt)}


def _persist(tmp_path, items=CASES, *, pdf_binaries=None, candidate_json=None, receipt_json=None, database=None):
    """Retain original candidates/receipts; do not create a PDF approval or core link."""
    database = database or _database(tmp_path)
    with sqlite3.connect(database) as db:
        for item in items:
            candidate, review = item["candidate"], _review(item)
            snapshot = candidate["snapshot"]
            assert digest(snapshot) == candidate["candidate_sha256"]
            rows = {"sources": snapshot["sources"], **snapshot["dependencies"], **snapshot["tables"]}
            pointers = []
            for table in ("sources", "articles", "article_versions", "article_content_versions", "article_enrichments",
                    "pdf_intake_documents", "pdf_intake_document_sources", "pdf_intake_articles", "pdf_intake_article_occurrences"):
                for original in rows.get(table, []):
                    row = dict(original)
                    if table == "articles":
                        pointers.append((row.get("current_version_id"), row.get("current_content_version_id"), row["article_id"]))
                        row.update(current_version_id=None, current_content_version_id=None)
                    if table == "pdf_intake_documents":
                        row["original_pdf"] = (pdf_binaries or {}).get(row["document_sha256"])
                    db.execute('INSERT OR REPLACE INTO "' + table + '" (' + ','.join('"' + key + '"' for key in row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
            db.executemany("UPDATE articles SET current_version_id=?,current_content_version_id=? WHERE article_id=?", pointers)
            sha = candidate["candidate_sha256"]
            db.execute("INSERT INTO registry_candidates VALUES(?,?,?,?,?,?)", (sha, "article", candidate["entity_id"], candidate_json or json.dumps(snapshot), item["receipt"]["reviewed_at"], "owner-agent-information-check.v1"))
            if receipt_json:
                review["receipt_json"] = receipt_json
            db.execute("INSERT INTO registry_reviews VALUES(?,?,?,?,?)", tuple(review[key] for key in ("review_id", "candidate_sha256", "status", "reviewed_at", "receipt_json")))
            db.execute("INSERT INTO registry_publication(entity_kind,entity_id,latest_candidate_sha256,published_candidate_sha256,is_visible) VALUES('article',?,?,?,1)", (candidate["entity_id"], sha, sha))
    return database


def test_original_inputs_and_native_empty_scope_are_exact():
    for item in CASES:
        assert digest(item["candidate"]["snapshot"]) == item["candidate"]["candidate_sha256"]
        verdict = evaluate_snapshot(item["candidate"]["snapshot"], reviews=[_review(item)])
        assert not verdict["eligible"] and verdict["primary"] is None
    native = evaluate_snapshot(APPROVED["candidate"]["snapshot"], reviews=[_review(APPROVED)])["observations"]["core:" + APPROVED["candidate"]["entity_id"]]
    assert not native["verified"] and native["reasons"] == ["owner_public_scope_unsupported"]


def test_exact_reviewed_pdf_scope_has_one_pdf_view_and_no_native_article(tmp_path):
    item = APPROVED
    snapshot = deepcopy(item["candidate"]["snapshot"])
    frozen = deepcopy(snapshot)
    verdict = evaluate_snapshot(snapshot, reviews=[_review(item)])
    view = permitted_view(snapshot, verdict)
    assert view is not None and view["entity_kind"] == "pdf_article"
    assert not view["tables"].get("articles")
    assert not view["tables"].get("article_versions")
    assert not view["tables"].get("article_content_versions")
    assert snapshot == frozen
    database = _persist(tmp_path)
    with sqlite3.connect(database) as db:
        values = published_snapshots(db, ("article", "pdf_article"))
        assert set(values) == {("pdf_article", view["entity_id"])}
        assert not db.execute("SELECT 1 FROM registry_publication WHERE entity_kind='pdf_article'").fetchone()
        assert db.execute("SELECT core_article_id,confirmation_basis FROM pdf_intake_articles WHERE article_id=?", (view["entity_id"],)).fetchone() == (None, None)
    reader = _reader(database, tmp_path)
    with reader.public_snapshot(direct=True):
        assert reader.articles(page_size=100)["items"] == []
        pdfs = reader.pdf_articles_all()
        assert len(pdfs) == 1
        actual = pdfs[0]
        assert actual["source_kind"] == "pdf" and actual.get("core_article_id") is None
        assert actual["summary"] == "The article says nature-negative activities received $7.3 trillion in 2023, compared with about $220 billion for nature-based solutions, and calls for redesigning the incentives and institutions shaping finance flows."
        assert actual["publication_date"] == "2026-09-14"
        assert actual["published_candidate_sha256"] == item["candidate"]["candidate_sha256"]
        assert actual["record_reference"]["entity_kind"] == "article"
        assert actual["record_reference"]["entity_id"] == item["candidate"]["entity_id"]
        assert len(actual["occurrences"]) == 2
        with pytest.raises(RegistryNotFoundError):
            reader.article(item["candidate"]["entity_id"])


@pytest.mark.parametrize("item", HOLDS, ids=lambda item: item["candidate"]["entity_id"])
def test_acquisition_only_controls_never_gain_a_public_view(tmp_path, item):
    snapshot = item["candidate"]["snapshot"]
    assert permitted_view(snapshot, evaluate_snapshot(snapshot, reviews=[_review(item)])) is None
    database = _persist(tmp_path, [item])
    with sqlite3.connect(database) as db:
        assert published_snapshots(db, ("article", "pdf_article")) == {}
    reader = _reader(database, tmp_path)
    with reader.public_snapshot(direct=True):
        assert reader.pdf_articles_all() == []
        with pytest.raises(RegistryNotFoundError):
            reader.article(item["candidate"]["entity_id"])


def test_pdf_only_scope_materializes_without_publishing_a_native_article(tmp_path, monkeypatch):
    database = _persist(tmp_path)
    from climate_registry.display import refresh_display
    with sqlite3.connect(database) as db:
        before = db.execute("SELECT * FROM registry_publication ORDER BY entity_id").fetchall()
        refresh_display(db)
        rows = db.execute("SELECT kind,identity,dto FROM display_details WHERE kind IN ('article','pdf_article')").fetchall()
        assert len(rows) == 1 and rows[0][0] == "pdf_article"
        payload = json.loads(rows[0][2])
        assert payload["record_reference"]["entity_kind"] == "article"
        assert payload["record_reference"]["candidate_sha256"] == APPROVED["candidate"]["candidate_sha256"]
        assert payload.get("core_article_id") is None and not payload.get("available_content")
        assert db.execute("SELECT * FROM registry_publication ORDER BY entity_id").fetchall() == before
        assert not published_snapshots(db, ("article",))
        assert set(published_snapshots(db, ("pdf_article",), entity_ids={"pdf_article": [payload["article_id"]]})) == {("pdf_article", payload["article_id"])}
    import api_server
    from fastapi.testclient import TestClient
    reader = _reader(database, tmp_path)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    api_server._clear_registry_list_cache()
    client = TestClient(api_server.app)
    result = client.get("/api/registry/pdf-intake/articles/" + payload["article_id"])
    assert result.status_code == 200 and result.json()["summary"] == payload["summary"]
    assert client.get("/api/registry/articles/" + APPROVED["candidate"]["entity_id"]).status_code == 404
    for item in HOLDS:
        assert client.get("/api/registry/pdf-intake/articles/" + item["candidate"]["entity_id"]).status_code == 404


def test_existing_format6_generation_is_rebuilt_by_normal_migration(tmp_path):
    from climate_registry.display import DisplayReader, refresh_display
    from climate_registry.publication import migrate_publication
    from climate_registry.read_api import RegistryContractError

    database = _persist(tmp_path)
    with sqlite3.connect(database) as db:
        refresh_display(db)
        approved = db.execute("SELECT * FROM registry_candidates ORDER BY candidate_sha256").fetchall()
        receipts = db.execute("SELECT * FROM registry_reviews ORDER BY review_id").fetchall()
        pointers = db.execute("SELECT * FROM registry_publication ORDER BY entity_id").fetchall()
        generation = db.execute("SELECT generation FROM display_state").fetchone()[0]
        # The prior renderer left this lawful PDF scope out of its ready format-6 cache.
        db.execute("DELETE FROM display_details WHERE kind='pdf_article'")
        db.execute("DELETE FROM display_pdf_articles")
        db.execute("UPDATE display_state SET format_version=6,ready=1,dirty=0")
    with sqlite3.connect(database) as db:
        with pytest.raises(RegistryContractError, match="display generation is unavailable"):
            DisplayReader(db)
    migrate_publication(database, tmp_path / "backups", apply=True)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT generation FROM display_state").fetchone()[0] == generation + 1
        assert db.execute("SELECT count(*) FROM display_pdf_articles").fetchone()[0] == 1
        assert db.execute("SELECT kind FROM display_details").fetchall() == [("pdf_article",)]
        assert db.execute("SELECT * FROM registry_candidates ORDER BY candidate_sha256").fetchall() == approved
        assert db.execute("SELECT * FROM registry_reviews ORDER BY review_id").fetchall() == receipts
        assert db.execute("SELECT * FROM registry_publication ORDER BY entity_id").fetchall() == pointers
        DisplayReader(db)


@pytest.mark.parametrize("mutation", ["missing_receipt", "receipt_actor", "receipt_scope", "receipt_binding", "candidate_schema", "candidate_scope", "capture", "source_binding", "native_hold", "pdf_fields"])
def test_old_receipt_cannot_approve_changed_pdf_or_native_scope(mutation):
    snapshot, review = deepcopy(APPROVED["candidate"]["snapshot"]), _review(APPROVED)
    receipt = json.loads(review["receipt_json"])
    if mutation == "missing_receipt":
        reviews = []
    elif mutation.startswith("receipt_"):
        if mutation == "receipt_actor":
            receipt["actor"] = receipt["source_checks"][0]["actor"]
        elif mutation == "receipt_scope":
            receipt["schema_version"] = "owner-agent-identity-review.v1"
        else:
            receipt["source_checks"] = receipt["source_checks"][:-1]
        review.update(receipt_json=json.dumps(receipt), review_id=digest([review["candidate_sha256"], "pass", receipt]))
        reviews = [review]
    else:
        if mutation == "candidate_schema":
            snapshot["schema_version"] = "registry-public-candidate.v999"
        elif mutation == "candidate_scope":
            snapshot["enrichment_scope"] = {"scope": "complete_article_content_update"}
        elif mutation == "pdf_fields":
            snapshot["tables"]["pdf_intake_article_occurrences"][0]["occurrence_json"] += " "
        else:
            native = mutation == "native_hold"
            attempt = next(row for row in reversed(snapshot["tables"]["article_check_attempts"]) if bool(row.get("core_article_id")) == native and row["attempt_id"].startswith("owner-attempt-"))
            packet = json.loads(attempt["packet_json"])
            if mutation == "capture":
                packet["owner_observation"]["capture"]["text"] += "changed"
            elif mutation == "source_binding":
                packet["source_revision_sha256"] = "0" * 64
            else:
                packet["public_fields"] = {"title": "unreviewed native claim"}
            attempt["packet_json"] = json.dumps(packet)
        reviews = [review]
    verdict = evaluate_snapshot(snapshot, reviews=reviews)
    assert not verdict["eligible"] and permitted_view(snapshot, verdict) is None


@pytest.mark.parametrize("change", ["hidden", "candidate_json", "published_pointer", "receipt_json"])
def test_pdf_projection_requires_its_original_visible_candidate_and_receipt(tmp_path, change):
    from climate_registry.read_api import RegistryContractError
    snapshot = deepcopy(APPROVED["candidate"]["snapshot"])
    snapshot["entity_id"] = "changed"
    receipt = deepcopy(APPROVED["receipt"])
    receipt["actor"] = receipt["source_checks"][0]["actor"]
    database = _persist(tmp_path, [APPROVED], candidate_json=json.dumps(snapshot) if change == "candidate_json" else None,
        receipt_json=json.dumps(receipt) if change == "receipt_json" else None)
    sha = APPROVED["candidate"]["candidate_sha256"]
    with sqlite3.connect(database) as db:
        if change == "hidden":
            db.execute("UPDATE registry_publication SET is_visible=0")
        elif change == "published_pointer":
            with pytest.raises(sqlite3.IntegrityError, match="published candidate requires approval"):
                db.execute("UPDATE registry_publication SET published_candidate_sha256=?", ("0" * 64,))
    reader = _reader(database, tmp_path)
    if change == "candidate_json":
        with pytest.raises(RegistryContractError):
            reader.pdf_articles_all()
    elif change == "published_pointer":
        assert reader.pdf_articles_all()[0]["published_candidate_sha256"] == sha
    else:
        assert reader.pdf_articles_all() == []


def test_pdf_scope_source_selection_and_cache_keep_an_unrelated_approved_alias(tmp_path):
    from test_identity_relations import _seed, _manifest, _scope, _ingest, _approve
    database = _seed(tmp_path)
    manifest = _manifest(database, [("article", "unchecked")])
    manifest["aliases"] = [{"source": _scope(manifest["plan"]["participants"][1]), "same_object": True, "evidence_ids": ["semantic-proof"]}]
    _approve(database, tmp_path, _ingest(database, tmp_path, manifest))
    _persist(tmp_path, database=database)
    identity = APPROVED["candidate"]["entity_id"]
    with sqlite3.connect(database) as db:
        selected = published_snapshots(db, ("article", "pdf_article"), entity_ids={"article": [identity]})
        assert set(selected) == {("pdf_article", identity)}
    reader = _reader(database, tmp_path)
    with reader.public_snapshot(direct=True):
        assert reader.article("unchecked")["article_id"] == "good"
        reader._public_candidates("article", "pdf_article")
        selected = reader._public_candidates("article", "pdf_article", entity_ids={"article": [identity]})
        assert set(selected) == {("pdf_article", identity)}
        assert len(reader.pdf_articles_all()) == 1
        assert len(reader.articles()["items"]) == 1


@pytest.mark.parametrize("state", ["pending", "approved", "hidden"])
def test_a_real_pdf_publication_governs_the_current_pdf_without_duplicate_or_revival(tmp_path, state):
    from test_owner_checks import _manifest, _ingest, _review, approve_owner_check
    database = _persist(tmp_path, [APPROVED])
    identity = APPROVED["candidate"]["entity_id"]
    occurrence = APPROVED["candidate"]["snapshot"]["tables"]["pdf_intake_article_occurrences"][0]["occurrence_id"]
    manifest = _manifest(database, kind="pdf_article", identity=identity, occurrence=occurrence)
    staged = _ingest(database, tmp_path, manifest)
    if state != "pending":
        receipt = _review(database, tmp_path, staged["candidate_sha256"])
        approve_owner_check(database, receipt, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path / "queue", backup_dir=tmp_path / "backups", apply=True)
    if state == "hidden":
        with sqlite3.connect(database) as db:
            db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_kind='pdf_article'")
    reader = _reader(database, tmp_path)
    with reader.public_snapshot(direct=True):
        assert reader.articles()["items"] == []
        pdfs = reader.pdf_articles_all()
        assert len(pdfs) == (0 if state == "hidden" else 1)
        if state == "pending":
            assert pdfs[0]["published_candidate_sha256"] == APPROVED["candidate"]["candidate_sha256"]
            assert pdfs[0]["record_reference"]["entity_kind"] == "article"
        elif state == "approved":
            assert pdfs[0]["published_candidate_sha256"] == staged["candidate_sha256"]
            assert pdfs[0]["record_reference"]["entity_kind"] == "pdf_article"
            assert pdfs[0]["summary"] == manifest["observations"][0]["public_fields"]["summary"]
            assert pdfs[0].get("core_article_id") is None
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id=?", (identity,)).fetchone()[0] == APPROVED["candidate"]["candidate_sha256"]
