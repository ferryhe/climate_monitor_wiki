import json
import sqlite3
from copy import deepcopy

import pytest

from climate_monitor.taxonomy import load_article_taxonomy
from climate_registry.category_correction import correct_article_categories
from climate_registry.errors import RegistryInputError
from climate_registry.publication import _approve, stage_entities, stage_snapshot, snapshot_entity, digest, export_public_snapshot
from climate_registry.read_api import RegistryReader
from test_registry_publication import _database, _pdf, _reader


def fixture(tmp_path, *, large=False, period=(None, None), linked_pdf=False):
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        if large:
            db.execute("UPDATE article_versions SET observed_summary=? WHERE article_id='a'", ("Retained source text. " * 200_000,))
        _pdf(db, identity="pdf-c", url="https://pdf.example/c",
            occurrence={"summary": "Original PDF observation", "summary_basis": "verbatim_pdf_article_row", "report_date": "2026-08-01"}, period=period)
        if linked_pdf:
            db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at,core_article_id,confirmation_basis) VALUES('pdf-a','https://example.org/a','Linked PDF title','2026-08-01','a','exact_url_eligible_detail')")
            db.execute("INSERT INTO pdf_intake_article_occurrences VALUES('linked-occurrence','pdf-a',?,3,'https://example.org/a',NULL,NULL,?,?,?)",
                ("f" * 64, "b" * 64, "c" * 64, json.dumps({"summary": "Linked PDF observation", "summary_basis": "verbatim_pdf_article_row"})))
        db.execute("INSERT INTO pdf_intake_document_sources VALUES(?,?,?,?)", (
            "f" * 64, "/archive/source.pdf", "source.pdf", "2026-08-01T00:00:00Z"))
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "fixture"}, status="accepted_legacy")
        db.execute("UPDATE article_versions SET observed_title='Pending title' WHERE version_id='v-a'")
        stage_entities(db)
        bindings = {(row[0], row[1]): {"is_visible": row[2], "published_candidate_sha256": row[3], "latest_candidate_sha256": row[4]}
            for row in db.execute("SELECT entity_kind,entity_id,is_visible,published_candidate_sha256,latest_candidate_sha256 FROM registry_publication")}
    reader = _reader(database, tmp_path)
    with reader.public_snapshot(direct=True):
        items = [reader.article("a"), reader.pdf_article("pdf-c")]
    taxonomy = load_article_taxonomy()
    plan = {"schema_version": "registry-category-correction.v1", "taxonomy": {
        "taxonomy_id": taxonomy.taxonomy_id, "sha256": taxonomy.sha256, "definition_yaml": taxonomy.definition_yaml},
        "records": [{"entity_kind": item["record_reference"]["entity_kind"], "entity_id": item["article_id"],
            "canonical_url": item["canonical_url"], "expected_binding": bindings[(item["record_reference"]["entity_kind"], item["article_id"])],
            "expected_categories": item.get("categories") or [], "categories": ["Physical Risk", "Climate & Health"],
            "evidence_basis": "Retained climate source evidence."} for item in items]}
    manifest = tmp_path / "plan.json"
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    return database, manifest, plan, bindings


def test_large_candidate_writes_reuse_the_reader_transaction(tmp_path):
    database, manifest, _, _ = fixture(tmp_path, large=True)
    result = correct_article_categories(database=database, manifest=manifest, apply=True, backup_dir=tmp_path / "backups")
    assert result["status"] == "applied" and result["changed"] == 2


@pytest.mark.parametrize("kind,identity", [("article", "a"), ("pdf_article", "pdf-c")])
def test_unchanged_restage_retains_categories_but_changed_content_does_not(tmp_path, kind, identity):
    database, manifest, plan, bindings = fixture(tmp_path)
    # Give the core article the same approved business content and no newer pending input.
    with sqlite3.connect(database) as db:
        db.execute("UPDATE article_versions SET observed_title='Title a' WHERE version_id='v-a'")
        db.execute("UPDATE registry_publication SET latest_candidate_sha256=published_candidate_sha256 WHERE entity_kind='article' AND entity_id='a'")
    plan["records"][0]["expected_binding"]["latest_candidate_sha256"] = bindings[("article", "a")]["published_candidate_sha256"]
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    correct_article_categories(database=database, manifest=manifest, apply=True, backup_dir=tmp_path / "backups")
    with sqlite3.connect(database) as db:
        approved = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", (kind, identity)).fetchone()[0]
        unchanged = snapshot_entity(db, kind, identity, annotations={}, source_dir=tmp_path / "sources")
        assert not any(row["source_ref"].startswith("category-correction:") for row in unchanged["tables"]["knowledge_versions"])
        restaged = stage_snapshot(db, unchanged)
        assert unchanged["derived_display"]["categories"] == ["Physical Risk", "Climate & Health"]
        assert restaged == approved
        if kind == "article":
            db.execute("UPDATE article_versions SET observed_summary='New business summary' WHERE version_id='v-a'")
        else:
            db.execute("UPDATE pdf_intake_articles SET title='New business title' WHERE article_id='pdf-c'")
        changed = snapshot_entity(db, kind, identity, annotations={}, source_dir=tmp_path / "sources")
        assert stage_snapshot(db, changed) != approved
        assert "derived_display" not in changed


def test_unchanged_pending_restage_does_not_borrow_corrected_public_categories(tmp_path):
    database, manifest, _, bindings = fixture(tmp_path)
    pending = bindings[("article", "a")]["latest_candidate_sha256"]
    correct_article_categories(database=database, manifest=manifest, apply=True, backup_dir=tmp_path / "backups")
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", "a", annotations={}, source_dir=tmp_path / "sources")
        assert stage_snapshot(db, snapshot) == pending
        assert "derived_display" not in snapshot
        db.execute("UPDATE articles SET last_seen='2026-09-08' WHERE article_id='a'")
        observed = snapshot_entity(db, "article", "a", annotations={}, source_dir=tmp_path / "sources")
        assert stage_snapshot(db, observed) != pending
        assert "derived_display" not in observed


@pytest.mark.parametrize("kind,identity", [("article", "a"), ("pdf_article", "pdf-c")])
def test_repeat_observation_retains_only_corrected_categories_through_approval(tmp_path, kind, identity):
    database, manifest, plan, bindings = fixture(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE article_versions SET observed_title='Title a' WHERE version_id='v-a'")
        db.execute("UPDATE registry_publication SET latest_candidate_sha256=published_candidate_sha256 WHERE entity_kind='article' AND entity_id='a'")
    plan["records"][0]["expected_binding"]["latest_candidate_sha256"] = bindings[("article", "a")]["published_candidate_sha256"]
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    correct_article_categories(database=database, manifest=manifest, apply=True, backup_dir=tmp_path / "backups")
    with sqlite3.connect(database) as db:
        sha = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", (kind, identity)).fetchone()[0]
        original = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
        original["derived_display"]["title"] = "Previous derived title"
        _approve(db, stage_snapshot(db, original), {"basis": "Unrelated display fixture"}, status="accepted_legacy")
        if kind == "article":
            db.execute("UPDATE articles SET last_seen='2026-09-08' WHERE article_id='a'")
            db.execute("UPDATE sources SET last_seen='2026-09-08' WHERE source_id='s'")
            db.execute("INSERT INTO reports VALUES('repeat','2026-09-08','repeat.md','Repeat report',?,'weekly','fixture',1,1,0,'[]')", ("b" * 64,))
            db.execute("INSERT INTO discoveries VALUES('repeat','repeat',1,'Updates','A','a','v-a','https://example.org/a','Title a','Observed summary',1,NULL)")
            db.execute("INSERT INTO report_appearances(report_id,article_id,version_id,discovery_id,section,pillar,ordinal,disposition) VALUES('repeat','a','v-a','repeat','Updates','A',1,'previously-seen')")
        else:
            db.execute("UPDATE pdf_intake_articles SET imported_at='2026-09-08' WHERE article_id='pdf-c'")
            db.execute("INSERT INTO pdf_intake_article_occurrences SELECT 'repeat',article_id,source_document_sha256,3,raw_url,'2026-09-08',publication_date,content_sha256,page_sha256,occurrence_json FROM pdf_intake_article_occurrences WHERE occurrence_id='occurrence'")
        updated = snapshot_entity(db, kind, identity, annotations={}, source_dir=tmp_path / "sources")
        candidate = stage_snapshot(db, updated)
        assert updated["derived_display"] == {"categories": ["Physical Risk", "Climate & Health"],
            "metadata_provenance": {"categories": "reviewed_category_correction"}}
        _approve(db, candidate, {"basis": "Repeat observation approval"}, status="accepted_legacy")
    reader = _reader(database, tmp_path)
    detail = reader.article(identity) if kind == "article" else reader.pdf_article(identity)
    assert detail["categories"] == ["Physical Risk", "Climate & Health"]
    assert detail["title"] != "Previous derived title"
    assert detail["published_candidate_sha256"] == candidate


@pytest.mark.parametrize("changed_field", ["title", "summary", "content", "enrichment"])
def test_semantic_changes_never_carry_corrected_categories(tmp_path, changed_field):
    database, manifest, plan, bindings = fixture(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE article_versions SET observed_title='Title a' WHERE version_id='v-a'")
        db.execute("UPDATE registry_publication SET latest_candidate_sha256=published_candidate_sha256 WHERE entity_kind='article' AND entity_id='a'")
    plan["records"][0]["expected_binding"]["latest_candidate_sha256"] = bindings[("article", "a")]["published_candidate_sha256"]
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    correct_article_categories(database=database, manifest=manifest, apply=True, backup_dir=tmp_path / "backups")
    with sqlite3.connect(database) as db:
        db.execute("UPDATE articles SET last_seen='2026-09-08' WHERE article_id='a'")
        if changed_field == "title":
            db.execute("UPDATE article_versions SET observed_title='New title' WHERE version_id='v-a'")
        elif changed_field == "summary":
            db.execute("UPDATE article_versions SET observed_summary='New summary' WHERE version_id='v-a'")
        elif changed_field == "content":
            db.execute("INSERT INTO article_content_versions VALUES('new-content','a',?,'New full body',?,'text/markdown',13,'fixture','v1','2026-09-08')", ("a" * 64, "b" * 64))
            db.execute("UPDATE articles SET current_content_version_id='new-content' WHERE article_id='a'")
        else:
            db.execute("INSERT INTO article_enrichments(enrichment_id,article_id,article_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('new-enrichment','a','v-a','complete','New summary','[\"Insurance Risk\"]','[\"climate\"]','en','manual','fixture','v1','2026-09-08')")
        snapshot = snapshot_entity(db, "article", "a", annotations={}, source_dir=tmp_path / "sources")
        stage_snapshot(db, snapshot)
        assert "derived_display" not in snapshot


def test_range_pdf_updates_freeze_current_categories_references_and_core_precedence(tmp_path, monkeypatch):
    from climate_registry.range_reports import freeze_range_report, load_range_report, render_range_report_html
    from climate_delivery.templates.adapters import adapt_range_report
    from climate_registry.publication import install_git_snapshot
    from climate_registry.schema import apply_migrations

    database, manifest, plan, _ = fixture(tmp_path, period=("2026-09-01", "2026-09-07"), linked_pdf=True)
    plan["records"][0]["categories"] = ["Physical Risk"]
    plan["records"][1]["categories"] = ["Climate & Health"]
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    correct_article_categories(database=database, manifest=manifest, apply=True, backup_dir=tmp_path / "backups")
    reader = _reader(database, tmp_path)
    frozen = freeze_range_report(reader, tmp_path / "ranges", start_date="2026-09-01", end_date="2026-09-07")
    updates = {item["pdf_article_id"]: item for item in frozen["pdf_source_updates"]}
    for pdf_id, detail in (("pdf-a", reader.article("a")), ("pdf-c", reader.pdf_article("pdf-c"))):
        item = updates[pdf_id]
        assert item["categories"] == detail["categories"]
        assert item["metadata_provenance"]["categories"] == "reviewed_category_correction"
        assert item["published_candidate_sha256"] == detail["published_candidate_sha256"]
        assert item["record_reference"] == detail["record_reference"]
    assert updates["pdf-a"]["categories"] == ["Physical Risk"]
    assert updates["pdf-c"]["categories"] == ["Climate & Health"]
    assert "Climate &amp; Health" in render_range_report_html(frozen)
    assert any(("Categories", "Climate & Health") in update.metadata for update in adapt_range_report(frozen).updates)

    wiki = tmp_path / "application/wiki"
    wiki.mkdir(parents=True)
    export_public_snapshot(database, wiki / "public-registry.json")
    rendered = tmp_path / "render.sqlite3"
    with sqlite3.connect(rendered) as db:
        apply_migrations(db)
    install_git_snapshot(rendered, wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", "1")
    static = freeze_range_report(_reader(rendered, tmp_path), tmp_path / "static-ranges", start_date="2026-09-01", end_date="2026-09-07")
    static_updates = {item["pdf_article_id"]: item for item in static["pdf_source_updates"]}
    for pdf_id in updates:
        for field in ("categories", "metadata_provenance", "published_candidate_sha256", "record_reference"):
            assert static_updates[pdf_id][field] == updates[pdf_id][field]

    with sqlite3.connect(database) as db:
        sha = updates["pdf-c"]["published_candidate_sha256"]
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
        snapshot["derived_display"]["categories"] = []
        replacement = stage_snapshot(db, snapshot)
        _approve(db, replacement, {"basis": "Explicit empty classification fixture"}, status="accepted_legacy")
    refreshed = freeze_range_report(reader, tmp_path / "ranges", start_date="2026-09-01", end_date="2026-09-07")
    assert next(item for item in refreshed["pdf_source_updates"] if item["pdf_article_id"] == "pdf-c")["categories"] == []
    assert load_range_report(tmp_path / "ranges", frozen["snapshot_id"])["pdf_source_updates"] == frozen["pdf_source_updates"]


def test_correction_is_category_only_preserves_pending_and_repeats(tmp_path):
    database, manifest, plan, bindings = fixture(tmp_path)
    before = database.read_bytes()
    result = correct_article_categories(database=database, manifest=manifest)
    assert result["status"] == "dry_run" and result["changed"] == 2
    assert database.read_bytes() == before and not (tmp_path / "backups").exists()
    result = correct_article_categories(database=database, manifest=manifest, apply=True, backup_dir=tmp_path / "backups")
    assert result["status"] == "applied"
    with sqlite3.connect(result["backup"]) as backup, sqlite3.connect(database) as db:
        assert backup.execute("SELECT count(*) FROM registry_candidates").fetchone()[0] + 2 == db.execute("SELECT count(*) FROM registry_candidates").fetchone()[0]
        for item in plan["records"]:
            key = item["entity_kind"], item["entity_id"]
            published, latest, visible = db.execute("SELECT published_candidate_sha256,latest_candidate_sha256,is_visible FROM registry_publication WHERE entity_kind=? AND entity_id=?", key).fetchone()
            original = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (bindings[key]["published_candidate_sha256"],)).fetchone()[0])
            updated = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (published,)).fetchone()[0])
            assert updated["derived_display"]["categories"] == item["categories"]
            assert updated["derived_display"]["metadata_provenance"]["categories"] == "reviewed_category_correction"
            allowed = deepcopy(updated)
            allowed.pop("derived_display")
            original.pop("derived_display", None)
            assert allowed == original
            assert visible == 1
            assert latest == (bindings[key]["latest_candidate_sha256"] if key == ("article", "a") else published)
        for table in ("articles", "article_versions", "pdf_intake_documents", "pdf_intake_document_sources", "pdf_intake_article_occurrences"):
            assert db.execute(f"SELECT * FROM {table}").fetchall() == backup.execute(f"SELECT * FROM {table}").fetchall()
    reader = _reader(database, tmp_path)
    with reader.public_snapshot(direct=True):
        assert reader.article("a")["categories"] == ["Physical Risk", "Climate & Health"]
        pdf = reader.pdf_article("pdf-c")
        assert pdf["categories"] == ["Physical Risk", "Climate & Health"]
        assert pdf["record_reference"]["candidate_sha256"] == pdf["published_candidate_sha256"]
        assert pdf["occurrences"][0]["source_observations"]
    exported = tmp_path / "public.json"
    export_public_snapshot(database, exported)
    payload = json.loads(exported.read_text())
    assert next(item for item in payload["articles"] if item["article_id"] == "a")["categories"] == ["Physical Risk", "Climate & Health"]
    exported_pdf = next(item for item in payload["pdf_articles"] if item["article_id"] == "pdf-c")
    assert exported_pdf["categories"] == pdf["categories"]
    assert exported_pdf["published_candidate_sha256"] == pdf["published_candidate_sha256"]
    rows_before = database.read_bytes()
    assert correct_article_categories(database=database, manifest=manifest, apply=True, backup_dir=tmp_path / "backups")["status"] == "already_applied"
    assert database.read_bytes() == rows_before


@pytest.mark.parametrize("field", ["visibility", "pending", "published"])
def test_stale_binding_rolls_back_all_records(tmp_path, field):
    database, manifest, plan, bindings = fixture(tmp_path)
    with sqlite3.connect(database) as db:
        if field == "visibility":
            db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id='pdf-c'")
        else:
            original = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",
                (bindings[("pdf_article", "pdf-c")]["published_candidate_sha256"],)).fetchone()[0])
            original["derived_display"] = {"title": "New approved PDF title"}
            replacement = stage_snapshot(db, original)
            if field == "published":
                _approve(db, replacement, {"basis": "replacement fixture"}, status="accepted_legacy")
    before = database.read_bytes()
    with pytest.raises(RegistryInputError, match="stale"):
        correct_article_categories(database=database, manifest=manifest, apply=True, backup_dir=tmp_path / "backups")
    assert database.read_bytes() == before


@pytest.mark.parametrize("categories", [["Unknown"], ["Physical Risk", "Physical Risk"],
    ["Physical Risk", "Transition Risk", "Insurance Risk", "Climate Risk", "Climate & Health"]])
def test_invalid_category_manifest_never_writes(tmp_path, categories):
    database, manifest, plan, _ = fixture(tmp_path)
    plan["records"][0]["categories"] = categories
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    before = database.read_bytes()
    with pytest.raises(RegistryInputError):
        correct_article_categories(database=database, manifest=manifest, apply=True, backup_dir=tmp_path / "backups")
    assert database.read_bytes() == before and not (tmp_path / "backups").exists()
