"""G4 exact semantic identity, source retention and approved-only relations."""
import json
import sqlite3
from copy import deepcopy

import pytest

from climate_registry.acquisition_review import digest
from climate_registry.identity import OVERLAY, SCOPE, _scope, _body
from climate_registry.owner_checks import plan_owner_check, ingest_owner_check, owner_review_template, approve_owner_check
from climate_registry.publication import canonical_entity, public_connection, published_snapshots, export_public_snapshot
from climate_registry.errors import RegistryInputError
from test_verified_publication import _database, _check, _approve_all, _reader
from test_owner_checks import _review


def _seed(tmp_path, *, acquisition_source=False):
    database = _database(tmp_path)
    if acquisition_source:
        from climate_registry.acquisition import _stable_id
        source_id = _stable_id("source", "example.org")
        with sqlite3.connect(database) as db:
            db.execute("UPDATE sources SET source_id=? WHERE source_id='s'", (source_id,))
            db.execute("UPDATE articles SET source_id=? WHERE source_id='s'", (source_id,))
    _check(database, tmp_path, ids={'core:good', 'core:unchecked'}, body='Title good. Summary good. Title unchecked. Summary unchecked. Same climate report landing and download.')
    _approve_all(database)
    return database


def _manifest(database, participants=(), *, identity="good"):
    manifest = plan_owner_check(database, 'article', identity, identity_correction=True, identity_participants=participants)
    ref = manifest['plan']['participants'][0]
    manifest.update(actor={'id': 'identity-author', 'task_id': 'identity-author-task'},
        authorization_reference='actual owner identity correction request', audit_reference='retained audit/identity', checked_at='2026-10-25T18:00:00Z',
        evidence=[{'id': 'semantic-proof', 'source': _scope(ref), 'text': 'Same climate report landing and download.',
            'excerpt': 'Same climate report', 'reason': 'The retained source explicitly identifies these representations.'}])
    return manifest


def _ingest(database, tmp_path, manifest):
    return ingest_owner_check(database, manifest, queue_dir=tmp_path/'queue', backup_dir=tmp_path/'backups', apply=True)


def _approve(database, tmp_path, result):
    receipt = _review(database, tmp_path, result['candidate_sha256'])
    receipt['reviewed_at'] = '2026-10-25T18:01:00Z'
    return approve_owner_check(database, receipt, lifecycle_root=tmp_path/'runs', queue_dir=tmp_path/'queue', backup_dir=tmp_path/'backups', apply=True)


def test_public_projection_alias_reuse_never_crosses_approval_or_revocation(tmp_path, monkeypatch):
    import climate_registry.publication as publication

    database = _seed(tmp_path)
    manifest = _manifest(database, [("article", "unchecked")])
    manifest["aliases"] = [{"source": _scope(manifest["plan"]["participants"][1]),
        "same_object": True, "evidence_ids": ["semantic-proof"]}]
    staged = _ingest(database, tmp_path, manifest)
    projections = []
    builds = []
    real_aliases = publication._identity_aliases

    def aliases(connection):
        if connection in projections:
            builds.append(connection)
        return real_aliases(connection)

    def project():
        with sqlite3.connect(database) as source:
            connection = public_connection(source)
        projections.append(connection)
        return connection

    def check(connection, expected):
        for _ in range(2):
            assert canonical_entity(connection, "article", "unchecked") == ("article", expected)
            assert publication.snapshot_metadata(connection, "article", "unchecked")["entity_id"] == expected

    monkeypatch.setattr(publication, "_identity_aliases", aliases)
    try:
        pending = project()
        check(pending, "unchecked")
        _approve(database, tmp_path, staged)
        approved = project()
        check(approved, "good")
        check(pending, "unchecked")
        alias = publication.snapshot_metadata(approved, "article", "good")["identity_mapping"]["aliases"][0]
        revoke = _manifest(database, [("article", "unchecked")])
        revoke["alias_operations"] = [{"action": "revoke", "alias_sha256": digest(alias), "evidence_ids": ["semantic-proof"]}]
        _approve(database, tmp_path, _ingest(database, tmp_path, revoke))
        revoked = project()
        check(revoked, "unchecked")
        check(approved, "good")
        assert builds == projections
        with sqlite3.connect(database) as source:
            # Mutable writer/source connections always reload the current graph.
            real = publication._identity_aliases
            calls = []
            def source_aliases(connection):
                calls.append(connection)
                return real(connection)
            monkeypatch.setattr(publication, "_identity_aliases", source_aliases)
            check_source = lambda: canonical_entity(source, "article", "unchecked")
            assert check_source() == check_source() == ("article", "unchecked")
            assert calls == [source, source]
    finally:
        for connection in projections:
            connection.close()


def test_mapping_pending_is_private_and_approved_alias_keeps_old_ids_and_receipts(tmp_path):
    database = _seed(tmp_path)
    manifest = _manifest(database, [('article', 'unchecked')])
    manifest['aliases'] = [{'source': _scope(manifest['plan']['participants'][1]), 'same_object': True, 'evidence_ids': ['semantic-proof']}]
    before = _reader(database, tmp_path).article('unchecked')
    with sqlite3.connect(database) as db:
        rows = {table: db.execute('SELECT * FROM '+table).fetchall() for table in ('articles', 'article_versions', 'article_check_attempts')}
    staged = _ingest(database, tmp_path, manifest)
    assert _reader(database, tmp_path).article('unchecked') == before
    assert len(_reader(database, tmp_path).articles()['items']) == 2
    _approve(database, tmp_path, staged)
    reader = _reader(database, tmp_path)
    assert len(reader.articles()['items']) == 1
    assert reader.article('unchecked')['article_id'] == 'good'
    assert reader.article('good')['identity_mapping']['approved_snapshot_sha256'] == staged['candidate_sha256']
    with sqlite3.connect(database) as db:
        for table, expected in rows.items():
            assert db.execute('SELECT * FROM '+table).fetchall() == expected
        approved = published_snapshots(db, ('article',))
        assert list(approved) == [('article', 'good')]
        assert approved[('article', 'good')]['identity_history'][0]['entity_id'] == 'unchecked'
        projection = public_connection(db)
        assert projection.execute('SELECT article_id FROM articles').fetchall() == [('good',)] or [row[0] for row in projection.execute('SELECT article_id FROM articles')] == ['good']
        projection.close()
    output = tmp_path/'export.json'
    export_public_snapshot(database, output)
    assert len(json.loads(output.read_text())['articles']) == 1
    assert json.loads(output.read_text())['articles'][0]['identity_mapping']['aliases'][0]['source']['entity_id'] == 'unchecked'


def test_identity_old_pass_scope_and_stale_source_reject_while_independent_item_continues(tmp_path):
    database = _seed(tmp_path)
    manifest = _manifest(database)
    result = _ingest(database, tmp_path, manifest)
    receipt = owner_review_template(database, result['candidate_sha256'])
    receipt.update(actor={'id': 'reviewer', 'task_id': 'review-task'}, authorization_reference='owner', audit_reference='audit', reviewed_at='2026-10-25T18:01:00Z', decision='pass', reason='Exact identity reviewed')
    bad = deepcopy(receipt); bad['identity_scope_sha256'] = '0'*64
    from climate_registry.identity import validate_review
    with sqlite3.connect(database) as db:
        snapshot = json.loads(db.execute('SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?', (result['candidate_sha256'],)).fetchone()[0])
    with pytest.raises(ValueError, match='exact-scope'):
        validate_review(snapshot, bad)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE articles SET last_seen='2026-10-25' WHERE article_id='good'")
    from climate_registry.item_lifecycle import record_items, partition_items
    record_items(tmp_path/'runs', database, [result['candidate_sha256']])
    with pytest.raises(RegistryInputError, match='held or invalid'):
        approve_owner_check(database, receipt, lifecycle_root=tmp_path/'runs', queue_dir=tmp_path/'queue')
    assert _reader(database, tmp_path).article('unchecked')['article_id'] == 'unchecked'


def test_same_url_materials_remain_distinct_and_url_only_match_fails_closed(tmp_path):
    database = _seed(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE articles SET canonical_url='https://example.org/good' WHERE article_id='unchecked'")
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES('pdf', 'https://example.org/good','Annual report','2026-10-25')")
        assert canonical_entity(db, 'pdf_article', 'pdf') == ('pdf_article', 'pdf')
        from climate_registry.schema import reconcile_pdf_article_links
        reconcile_pdf_article_links(db)
        assert db.execute("SELECT core_article_id FROM pdf_intake_articles WHERE article_id='pdf'").fetchone()[0] is None
        from climate_registry.acquisition import _article_identity
        with pytest.raises(ValueError, match='ambiguous URL'):
            _article_identity(db, {'canonical_url': 'https://example.org/good'})
        first = _article_identity(db, {'canonical_url': 'https://example.org/good', 'semantic_identity': {'key': '2026-report'}})
        second = _article_identity(db, {'canonical_url': 'https://example.org/good', 'semantic_identity': {'key': '2027-report'}})
        assert first != second
    # The old approved version is unchanged while different material is pending.
    assert len(_reader(database, tmp_path).articles()['items']) == 2



def test_two_pdf_parents_at_same_url_do_not_resolve_to_unique_core(tmp_path):
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        for identity in ("pdf-2026", "pdf-2027"):
            db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES(?, 'https://example.org/good',?,'2026-10-25')", (identity, identity))
        for identity in ("pdf-2026", "pdf-2027"):
            assert canonical_entity(db, "pdf_article", identity) == ("pdf_article", identity)


def test_two_pdf_parents_at_same_url_keep_separate_pending_candidates(tmp_path):
    from climate_registry.publication import stage_entities
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        for identity in ("pdf-2026", "pdf-2027"):
            db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES(?, 'https://example.org/good',?,'2026-10-25')", (identity, identity))
        staged = stage_entities(db, pdf_article_ids={"pdf-2026", "pdf-2027"})
        assert len(staged) == 2
        snapshots = [json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0]) for sha in staged]
        assert {(row["entity_kind"], row["entity_id"]) for row in snapshots} == {("pdf_article", "pdf-2026"), ("pdf_article", "pdf-2027")}
        assert {row["tables"]["pdf_intake_articles"][0]["title"] for row in snapshots} == {"pdf-2026", "pdf-2027"}



def test_explicit_pdf_core_link_precedes_url_and_pdf_parent_ambiguity(tmp_path):
    from climate_registry.publication import snapshot_entity
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at,core_article_id) VALUES('pdf-linked', 'https://example.org/good','Explicitly linked material','2026-10-25','unchecked')")
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES('pdf-other', 'https://example.org/good','Different material','2026-10-25')")
        assert canonical_entity(db, "pdf_article", "pdf-linked") == ("article", "unchecked")
        assert canonical_entity(db, "pdf_article", "pdf-other") == ("pdf_article", "pdf-other")
        assert [row["article_id"] for row in snapshot_entity(db, "article", "unchecked")["tables"]["pdf_intake_articles"]] == ["pdf-linked"]
        assert snapshot_entity(db, "article", "good")["tables"]["pdf_intake_articles"] == []


def test_mapping_cannot_edit_content_or_reuse_wrong_body_relation(tmp_path):
    database = _seed(tmp_path)
    manifest = _manifest(database)
    manifest['disposition'] = 'article_linked_to_dates'
    manifest['relations'] = [{'article_body': {'content_version_id': 'wrong', 'body_sha256': '0'*64}, 'event': {}, 'evidence_ids': ['semantic-proof']}]
    with pytest.raises(RegistryInputError, match='exact retained current body'):
        _ingest(database, tmp_path, manifest)
    manifest = _manifest(database)
    manifest['plan']['participants'][0]['snapshot']['tables']['article_versions'][0]['observed_summary'] = 'invented new summary'
    with pytest.raises(RegistryInputError, match='snapshot differs'):
        _ingest(database, tmp_path, manifest)


def test_pdf_publisher_does_not_become_organizer():
    from climate_monitor.meeting_fields import pdf_meeting_fields
    fields = pdf_meeting_fields({'publisher': 'Publisher', 'host': 'Host', 'partner': 'Partner', 'secretariat': 'Secretariat'})
    assert fields['organizer'] is None
    assert [fields[key] for key in ('publisher', 'host', 'partner', 'secretariat')] == ['Publisher', 'Host', 'Partner', 'Secretariat']
    assert pdf_meeting_fields({'publisher': 'Publisher', 'organizer': 'Actual organizer'})['organizer'] == 'Actual organizer'


def test_same_url_explicit_material_keys_persist_separate_bodies_and_origins(tmp_path):
    from test_issue112_acquisition import _database as acquisition_database, _item, _batch
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    database = acquisition_database(tmp_path)
    items = []
    for edition in ("2026", "2027"):
        item = _item(body=f"Independent climate report {edition}.", discovery_ref="report-" + edition, discovery_kind="site", discovery_search_ref=None)
        item["semantic_identity"] = {"key": "report-" + edition, "excerpt": "climate report " + edition,
            "reason": "The page identifies a separate annual report."}
        items.append(item)
    store_acquisition_batch(database, _batch(items, searches=[], search_decision={"status": "no_search", "reason": "site-only"}))
    stored = load_acquisition_batch(database, "batch-112")["items"]
    assert len(stored) == 2 and len({item["article_id"] for item in stored}) == 2
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*),count(DISTINCT canonical_url) FROM articles").fetchone() == (2, 1)
        assert db.execute("SELECT count(*) FROM article_content_versions").fetchone()[0] == 2
        raw = db.execute("SELECT origins_json FROM acquisition_items").fetchall()
        assert {json.loads(row[0])[0]["semantic_identity"]["key"] for row in raw} == {"report-2026", "report-2027"}
    with pytest.raises(ValueError, match="ambiguous URL"):
        store_acquisition_batch(database, _batch([_item()], batch_id="ambiguous-later"))


def test_body_bound_article_dates_relation_keeps_article_visible_and_old_relation(tmp_path):
    from test_verified_publication import _pdf_pair
    database = _seed(tmp_path)
    body = "Title good. Summary good. Same climate report landing and download. Conference 2027 announcement analysis."
    with sqlite3.connect(database) as db:
        sha = __import__("hashlib").sha256(body.encode()).hexdigest()
        db.execute("INSERT INTO article_content_versions VALUES('body-good','good',?,?,?,'text/html',NULL,'fixture','v1','2026-10-01T00:00:00Z')", (sha, body, sha))
        db.execute("UPDATE articles SET current_content_version_id='body-good' WHERE article_id='good'")
    event = _pdf_pair(database)
    _check(database, tmp_path, ids={"core:good"}, body=body)
    _check(database, tmp_path, kind="meetings", ids={"target-occ"}, candidate=event)
    _approve_all(database)
    manifest = _manifest(database, [("pdf_meeting", "target")])
    manifest["disposition"] = "article_linked_to_dates"
    manifest["relations"] = [{"article_body": _body(manifest["plan"]["participants"][0]["snapshot"]),
        "event": _scope(manifest["plan"]["participants"][1]), "evidence_ids": ["semantic-proof"]}]
    assert not _reader(database, tmp_path).article("good").get("article_event_relations")
    staged = _ingest(database, tmp_path, manifest)
    assert not _reader(database, tmp_path).article("good").get("article_event_relations")
    _approve(database, tmp_path, staged)
    item = _reader(database, tmp_path).article("good")
    assert item["article_event_relations"][0]["article_body"]["content_version_id"] == "body-good"
    assert item["article_event_relations"][0]["facts"]["start_date"] == "2027-06-10"
    assert not item["is_activity"]
    assert "good" in {item["article_id"] for item in _reader(database, tmp_path).articles()["items"]}
    # A new correction inherits history and gets a fresh receipt, never reuses the old PASS.
    next_manifest = _manifest(database)
    corrected = _ingest(database, tmp_path, next_manifest)
    assert corrected["candidate_sha256"] != staged["candidate_sha256"]
    _approve(database, tmp_path, corrected)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM registry_reviews WHERE candidate_sha256 IN (?,?)", (staged["candidate_sha256"], corrected["candidate_sha256"])).fetchone()[0] == 2
        assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (staged["candidate_sha256"],)).fetchone()


def test_same_object_mapping_retains_human_date_but_independent_material_does_not_inherit(tmp_path):
    from test_article_content_dates import _manual
    from test_owner_checks import _ingest as ingest_date
    database = _seed(tmp_path)
    original = _manual(database)
    ingest_date(database, tmp_path, original)
    before = _reader(database, tmp_path).article("good")["manual_date_overrides"]["publication_date"]
    manifest = _manifest(database)
    manifest["canonical_url"] = "https://example.org/moved-report"
    staged = _ingest(database, tmp_path, manifest)
    _approve(database, tmp_path, staged)
    current = _reader(database, tmp_path).article("good")
    assert current["publication_date"] == "2026-10-02"
    assert current["manual_date_overrides"]["publication_date"] == before
    assert current["canonical_url"] == "https://example.org/moved-report"
    assert not _reader(database, tmp_path).article("unchecked").get("manual_date_overrides")
    # Identity and date scopes compose in either order, using the existing G1 writer.
    replacement = _manual(database, fields={"publication_date": "2026-10-03"})
    ingest_date(database, tmp_path, replacement)
    assert _reader(database, tmp_path).article("good")["publication_date"] == "2026-10-03"
    # A date instruction cannot become identity/relation authority.
    with pytest.raises(ValueError, match="separate exact scopes"):
        plan_owner_check(database, "article", "good", identity_correction=True, date_fields=["publication_date"], human_date_override=True)


@pytest.mark.parametrize("failure", [False, True])
def test_schema27_retains_ids_rows_constraints_and_rolls_back_failed_migration(tmp_path, monkeypatch, failure):
    import climate_registry.schema as schema
    database = tmp_path / "history.sqlite3"
    with sqlite3.connect(database) as db:
        schema.apply_migrations(db, target_version=26)
        db.execute("INSERT INTO sources VALUES('s','example.org','Publisher','2026-01-01','2026-01-01')")
        db.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES('old','https://example.org/shared','s','2026-01-01','2026-01-01')")
        db.execute("INSERT INTO article_versions VALUES('old-version','old','Old','Old','Original','f','report-title-summary','2026-01-01','2026-01-01')")
        db.execute("UPDATE articles SET current_version_id='old-version' WHERE article_id='old'")
        db.commit()
        rows = {table: db.execute('SELECT rowid,* FROM '+table).fetchall() for table in ('articles', 'article_versions', 'sources')}
        triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
        if failure:
            original = schema._identity_url_migration
            monkeypatch.setattr(schema, '_identity_url_migration', lambda connection: original(connection) + '; SELECT missing_identity_migration_function();')
            with pytest.raises(sqlite3.OperationalError):
                schema.apply_migrations(db)
            assert db.execute('PRAGMA user_version').fetchone()[0] == 26
        else:
            assert schema.apply_migrations(db) == [27]
            db.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES('new','https://example.org/shared','s','2027-01-01','2027-01-01')")
            assert db.execute('SELECT article_id FROM articles WHERE canonical_url=?', ('https://example.org/shared',)).fetchall() == [('old',), ('new',)]
            db.rollback()
        for table, expected in rows.items():
            assert db.execute('SELECT rowid,* FROM '+table).fetchall() == expected
        assert db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall() == triggers
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1


def test_one_pdf_aggregate_can_bind_two_occurrences_to_distinct_articles(tmp_path):
    from test_registry_publication import _pdf
    from verified_fixtures import seed_verified_articles
    database = _seed(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db, identity="shared-pdf", url="https://example.org/shared", occurrence_id="pdf-first")
        row = list(db.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='pdf-first'").fetchone())
        row[0] = "pdf-second"
        row[3] = 2
        raw = json.loads(row[-1]); raw["occurrence_id"] = "pdf-second"; raw["page"] = 2
        row[-1] = json.dumps(raw)
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)", row)
        seed_verified_articles(db, occurrence_ids={"pdf-first", "pdf-second"})
    _approve_all(database)
    for target, occurrence in (("good", "pdf-first"), ("unchecked", "pdf-second")):
        manifest = _manifest(database, [("pdf_article", "shared-pdf")], identity=target)
        source = _scope(manifest["plan"]["participants"][1])
        source.update(selection="occurrences", occurrence_ids=[occurrence], content_version_id=None, body_sha256=None)
        manifest["aliases"] = [{"source": source, "same_object": True, "evidence_ids": ["semantic-proof"]}]
        _approve(database, tmp_path, _ingest(database, tmp_path, manifest))
    with sqlite3.connect(database) as db:
        views = published_snapshots(db, ("article", "pdf_article"))
        assert ("pdf_article", "shared-pdf") not in views
        assert [row["occurrence_id"] for row in views[("article", "good")]["tables"]["pdf_intake_article_occurrences"]] == ["pdf-first"]
        assert [row["occurrence_id"] for row in views[("article", "unchecked")]["tables"]["pdf_intake_article_occurrences"]] == ["pdf-second"]
        assert db.execute("SELECT occurrence_id,article_id FROM pdf_intake_article_occurrences ORDER BY occurrence_id").fetchall() == [("pdf-first", "shared-pdf"), ("pdf-second", "shared-pdf")]
    reader = _reader(database, tmp_path)
    assert len(reader.articles()["items"]) == 2
    assert [row["occurrence_id"] for row in reader.article("good")["pdf_occurrences"]] == ["pdf-first"]
    assert [row["occurrence_id"] for row in reader.article("unchecked")["pdf_occurrences"]] == ["pdf-second"]

    # PDF report and export keep both semantic modules under the original source IDs.
    report = reader.pdf_report("f" * 64)
    assert report["article_count"] == len(report["articles"]) == 2
    assert {item["canonical_article_id"] for item in report["articles"]} == {"good", "unchecked"}
    assert {item["source_pdf_article_id"] for item in report["articles"]} == {"shared-pdf"}
    output = tmp_path / "semantic-export.json"
    export_public_snapshot(database, output)
    exported = json.loads(output.read_text())
    report = exported["pdf_report_details"]["f" * 64]
    assert report["article_count"] == len(report["articles"]) == 2


def test_complete_content_approval_baselines_survive_identity_correction(tmp_path, monkeypatch):
    from test_enrichment_contract import _published_g3_kind
    from climate_registry.publication import snapshot_entity, stage_snapshot
    from climate_registry.enrichment_contract import reviewed_content_receipt
    from climate_registry.verification import review_context
    database, content_approval, summary = _published_g3_kind(tmp_path, monkeypatch, "complete")
    manifest = _manifest(database)
    manifest["evidence"][0].update(text="Flood losses reached 20 billion.", excerpt="20 billion")
    staged = _ingest(database, tmp_path, manifest)
    _approve(database, tmp_path, staged)
    assert _reader(database, tmp_path).article("good")["summary"] == summary
    with sqlite3.connect(database) as db:
        approved = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (staged["candidate_sha256"],)).fetchone()[0])
        assert reviewed_content_receipt(approved, review_context(db)[staged["candidate_sha256"]])["candidate_sha256"] == content_approval
        next_sha = stage_snapshot(db, snapshot_entity(db, "article", "good"))
        next_snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (next_sha,)).fetchone()[0])
        baseline = next_snapshot["enrichment_scope"]["approval_inheritance_baseline"]
        assert baseline["candidate_sha256"] == staged["candidate_sha256"]
        assert baseline["content_review"]["candidate_sha256"] == content_approval
        assert next_snapshot["approved_identity_history"][0]["snapshot"] == approved


@pytest.mark.parametrize("disposition", ["pure_event", "portal", "irrelevant"])
def test_pending_disposition_only_is_reviewed_without_publishing_source_facts(tmp_path, disposition):
    from climate_registry.publication import stage_entities, _approve as approve_fixture
    from climate_registry.verification import evaluate_snapshot, review_context
    database = _database(tmp_path)
    _check(database, tmp_path, ids={"core:good", "core:unchecked"}, body="Title good. Summary good. Title unchecked. Summary unchecked. Same climate report landing and download.")
    with sqlite3.connect(database) as db:
        stage_entities(db)
        independent = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_id='unchecked'").fetchone()[0]
        approve_fixture(db, independent, {"basis": "independently approved unrelated fixture"}, status="accepted_legacy")
        original = db.execute("SELECT * FROM article_versions").fetchall()
    manifest = _manifest(database)
    assert manifest["plan"]["participants"][0]["expected_state"]["published_candidate_sha256"] is None
    manifest["disposition"] = disposition
    staged = _ingest(database, tmp_path, manifest)
    assert {item["article_id"] for item in _reader(database, tmp_path).articles()["items"]} == {"unchecked"}
    _approve(database, tmp_path, staged)
    with sqlite3.connect(database) as db:
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (staged["candidate_sha256"],)).fetchone()[0])
        verdict = evaluate_snapshot(snapshot, reviews=review_context(db)[staged["candidate_sha256"]])
        assert not verdict["eligible"] and verdict["disposition_only"]
        assert verdict["reasons"] == ["reviewed_non_article_disposition"]
        assert db.execute("SELECT * FROM article_versions").fetchall() == original
        assert ("article", "good") not in published_snapshots(db, ("article",))
    assert {item["article_id"] for item in _reader(database, tmp_path).articles()["items"]} == {"unchecked"}
    assert _ingest(database, tmp_path, manifest)["status"] == "no-op"
    output = tmp_path / "disposition-export.json"
    export_public_snapshot(database, output)
    assert {item["article_id"] for item in json.loads(output.read_text())["articles"]} == {"unchecked"}


@pytest.mark.parametrize("mutation", ["article", "url", "alias", "relation", "source_changed", "old_pass"])
def test_pending_disposition_cannot_expand_to_facts_or_reuse_stale_scope(tmp_path, mutation):
    from climate_registry.publication import stage_entities
    database = _database(tmp_path)
    _check(database, tmp_path, ids={"core:good"}, body="Title good. Summary good. Same climate report landing and download.")
    with sqlite3.connect(database) as db:
        stage_entities(db)
    manifest = _manifest(database)
    manifest["disposition"] = "portal"
    if mutation == "article":
        manifest["disposition"] = "article"
    elif mutation == "url":
        manifest["canonical_url"] = "https://example.org/another"
    elif mutation == "alias":
        manifest["aliases"] = [{"source": {}, "same_object": True, "evidence_ids": ["semantic-proof"]}]
    elif mutation == "relation":
        manifest["relations"] = [{"article_body": {}, "event": {}, "evidence_ids": ["semantic-proof"]}]
    if mutation in {"article", "url", "alias", "relation"}:
        with pytest.raises(RegistryInputError, match="disposition-only"):
            _ingest(database, tmp_path, manifest)
        return
    staged = _ingest(database, tmp_path, manifest)
    receipt = _review(database, tmp_path, staged["candidate_sha256"])
    receipt["reviewed_at"] = "2026-10-25T18:01:00Z"
    if mutation == "source_changed":
        with sqlite3.connect(database) as db:
            db.execute("UPDATE articles SET last_seen='2026-10-26' WHERE article_id='good'")
        with pytest.raises(RegistryInputError, match="held or invalid"):
            approve_owner_check(database, receipt, lifecycle_root=tmp_path/'runs', queue_dir=tmp_path/'queue')
    else:
        receipt["identity_scope_sha256"] = "0" * 64
        with pytest.raises(RegistryInputError, match="exact-scope"):
            approve_owner_check(database, receipt, lifecycle_root=tmp_path/'runs', queue_dir=tmp_path/'queue')


def test_approved_alias_routes_later_capture_into_same_canonical_identity(tmp_path):
    from climate_registry.acquisition import _article_identity
    database = _seed(tmp_path)
    manifest = _manifest(database, [("article", "unchecked")])
    manifest["aliases"] = [{"source": _scope(manifest["plan"]["participants"][1]), "same_object": True, "evidence_ids": ["semantic-proof"]}]
    _approve(database, tmp_path, _ingest(database, tmp_path, manifest))
    with sqlite3.connect(database) as db:
        assert _article_identity(db, {"canonical_url": "https://example.org/unchecked"}) == "good"
        assert _article_identity(db, {"canonical_url": "https://example.org/good"}) == "good"
        assert db.execute("SELECT article_id,canonical_url FROM articles ORDER BY article_id").fetchall() == [("good", "https://example.org/good"), ("unchecked", "https://example.org/unchecked")]


@pytest.mark.parametrize("stale", [False, True])
def test_existing_prepare_owner_routes_exact_disposition_input_and_independent_item(tmp_path, stale):
    from climate_registry.owner_checks import prepare_owner_checks
    from test_owner_checks import _manifest as source_manifest
    database = _database(tmp_path)
    _check(database, tmp_path, ids={"core:good", "core:unchecked"}, body="Title good. Summary good. Title unchecked. Summary unchecked. Same climate report landing and download.")
    manifest = _manifest(database)
    manifest["disposition"] = "portal"
    independent = source_manifest(database, kind="article", identity="unchecked", occurrence="core:unchecked")
    if stale:
        manifest["plan"]["participants"][0]["source_snapshot_sha256"] = "0" * 64
    result = prepare_owner_checks(database, [manifest, independent], root=tmp_path / "runs")
    assert independent in result["ready"]
    if stale:
        assert manifest not in result["ready"] and len(result["invalid"]) == 1
        assert "stale" in result["invalid"][0]["reason"]
        return
    assert manifest in result["ready"] and not result["invalid"]
    staged = _ingest(database, tmp_path, manifest)
    # Preparation retained the exact original input; no second lifecycle registration.
    receipt = owner_review_template(database, staged["candidate_sha256"])
    receipt.update(actor={"id": "prepared-independent-reviewer", "task_id": "prepared-review-task"},
        authorization_reference="actual owner correction", audit_reference="audit/prepared-review",
        reviewed_at="2026-10-25T18:01:00Z", decision="pass", reason="Reviewed exact prepared source and disposition scope")
    assert approve_owner_check(database, receipt, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path / "queue",
        backup_dir=tmp_path / "backups", apply=True)["status"] == "approved"
    assert not _reader(database, tmp_path).articles()["items"]


def _merge_identity(database, tmp_path, participants, *, identity="good"):
    manifest = _manifest(database, participants, identity=identity)
    manifest["aliases"] = [{"source": _scope(ref), "same_object": True, "evidence_ids": ["semantic-proof"]}
        for ref in manifest["plan"]["participants"][1:]]
    return _approve(database, tmp_path, _ingest(database, tmp_path, manifest))


def _identity_pdf_sources(database, count=1):
    from test_registry_publication import _pdf
    from verified_fixtures import seed_verified_articles
    with sqlite3.connect(database) as db:
        _pdf(db, identity="pdf-first", url="https://example.org/download", occurrence_id="occ-first")
        if count == 2:
            db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES('pdf-second','https://example.org/second-download','Second source link','2026-08-01')")
            row = list(db.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='occ-first'").fetchone())
            row[0], row[1], row[3], row[4] = "occ-second", "pdf-second", 3, "https://example.org/second-download"
            original = json.loads(row[-1]); original.update(occurrence_id=row[0], page=row[3], raw_url=row[4])
            row[-1] = json.dumps(original)
            db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)", row)
            seed_verified_articles(db, occurrence_ids={"occ-second"})
    _approve_all(database)


def test_later_locator_correction_preserves_cumulative_alias_and_occurrence_ownership(tmp_path):
    from climate_registry.acquisition import _article_identity
    database = _seed(tmp_path)
    _identity_pdf_sources(database)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES('third','https://example.org/third','s','2026-09-01','2026-09-01')")
        db.execute("INSERT INTO article_versions VALUES('v-third','third','Title third','Title third','Summary third','f-third','report-title-summary','2026-09-01','2026-09-01')")
        db.execute("UPDATE articles SET current_version_id='v-third' WHERE article_id='third'")
    _check(database, tmp_path, ids={"core:third"}, body="Title third. Summary third. Same climate report landing and download.")
    _approve_all(database)
    _merge_identity(database, tmp_path, [("article", "unchecked"), ("pdf_article", "pdf-first")])
    with sqlite3.connect(database) as db:
        originals = {table: db.execute("SELECT * FROM " + table).fetchall() for table in ("articles", "article_versions", "pdf_intake_articles", "pdf_intake_article_occurrences")}
    manifest = _manifest(database)
    manifest["canonical_url"] = "https://example.org/moved-report"
    staged = _ingest(database, tmp_path, manifest)
    assert _reader(database, tmp_path).article("good")["canonical_url"] == "https://example.org/good"
    _approve(database, tmp_path, staged)
    reader = _reader(database, tmp_path)
    assert {row["article_id"] for row in reader.articles()["items"]} == {"good", "third"}
    assert reader.article("unchecked")["article_id"] == "good"
    assert [row["occurrence_id"] for row in reader.article("good")["pdf_occurrences"]] == ["occ-first"]
    with sqlite3.connect(database) as db:
        assert _article_identity(db, {"canonical_url": "https://example.org/unchecked"}) == "good"
        for table, rows in originals.items():
            assert db.execute("SELECT * FROM " + table).fetchall() == rows
    output = tmp_path/"cumulative.json"; export_public_snapshot(database, output)
    assert {row["article_id"] for row in json.loads(output.read_text())["articles"]} == {"good", "third"}
    # A third exact correction moves the whole existing group into another Article.
    _merge_identity(database, tmp_path, [("article", "good")], identity="third")
    assert _reader(database, tmp_path).article("unchecked")["article_id"] == "third"
    assert _reader(database, tmp_path).pdf_article("pdf-first")["core_article_id"] == "third"
    with sqlite3.connect(database) as db:
        assert _article_identity(db, {"canonical_url": manifest["canonical_url"]}) == "third"


def test_approved_locator_reuses_acquisition_identity_without_overriding_material_keys(tmp_path):
    from climate_registry.acquisition import _article_identity, store_acquisition_batch, load_acquisition_batch
    from test_issue112_acquisition import _item, _batch
    database = _seed(tmp_path, acquisition_source=True)
    manifest = _manifest(database); manifest["canonical_url"] = "https://example.org/moved-report"
    _approve(database, tmp_path, _ingest(database, tmp_path, manifest))
    url = manifest["canonical_url"]
    with sqlite3.connect(database) as db:
        assert _article_identity(db, {"canonical_url": url}) == "good"
        assert _article_identity(db, {"canonical_url": url, "semantic_identity": {"key": "different-edition"}}) != "good"
    item = _item(url, discovery_kind="site", discovery_ref="moved", discovery_search_ref=None)
    store_acquisition_batch(database, _batch([item], searches=[], search_decision={"status": "no_search", "reason": "site-only"}))
    assert load_acquisition_batch(database, "batch-112")["items"][0]["article_id"] == "good"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT canonical_url FROM articles WHERE article_id='good'").fetchone()[0] == "https://example.org/good"
        db.execute("UPDATE articles SET canonical_url=? WHERE article_id='unchecked'", (url,))
        with pytest.raises(ValueError, match="ambiguous URL"):
            _article_identity(db, {"canonical_url": url})


@pytest.mark.parametrize("hidden_id", ["good", "unchecked"])
def test_visibility_through_canonical_or_old_alias_hides_and_restores_whole_group(tmp_path, hidden_id):
    from climate_registry.publication import set_visibility
    from climate_registry.read_api import RegistryNotFoundError
    database = _seed(tmp_path)
    _merge_identity(database, tmp_path, [("article", "unchecked")])
    set_visibility(database, "article", hidden_id, False)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM registry_publication WHERE is_visible=1").fetchone()[0] == 0
        # Retained ownership also blocks a stale visibility bit restored on the alias.
        db.execute("UPDATE registry_publication SET is_visible=1 WHERE entity_kind='article' AND entity_id='unchecked'")
    reader = _reader(database, tmp_path)
    assert not reader.articles()["items"]
    with sqlite3.connect(database) as db:
        assert not published_snapshots(db, ("article",))
        projection = public_connection(db)
        assert projection.execute("SELECT count(*) FROM articles").fetchone()[0] == 0
        projection.close()
    for identity in ("good", "unchecked"):
        with pytest.raises(RegistryNotFoundError): reader.article(identity)
    output = tmp_path/"hidden.json"; export_public_snapshot(database, output)
    assert not json.loads(output.read_text())["articles"]
    set_visibility(database, "article", "unchecked", True)
    assert [row["article_id"] for row in reader.articles()["items"]] == ["good"]


def test_old_pdf_id_at_different_url_resolves_approved_exact_parent_scope(tmp_path):
    from climate_registry.read_api import RegistryReader
    database = _seed(tmp_path); _identity_pdf_sources(database, count=2)
    _merge_identity(database, tmp_path, [("pdf_article", "pdf-first"), ("pdf_article", "pdf-second")])
    reader = _reader(database, tmp_path)
    for identity, occurrence in (("pdf-first", "occ-first"), ("pdf-second", "occ-second")):
        payload = reader.pdf_article(identity)
        assert payload["article_id"] == identity and payload["core_article_id"] == "good"
        assert [row["occurrence_id"] for row in payload["occurrences"]] == [occurrence]
        assert [row["occurrence_id"] for row in reader.pdf_article(identity, allowed_occurrence_ids={occurrence})["occurrences"]] == [occurrence]
    output = tmp_path/"application"/"wiki"/"public-registry.json"; output.parent.mkdir(parents=True)
    export_public_snapshot(database, output)
    static = RegistryReader.from_public_export(tmp_path/"application")
    from climate_registry.read_api import RegistryNotFoundError
    for scoped_reader in (reader, static):
        assert [row["occurrence_id"] for row in scoped_reader.pdf_article("pdf-first", allowed_occurrence_ids={"occ-first"})["occurrences"]] == ["occ-first"]
        with pytest.raises(RegistryNotFoundError):
            scoped_reader.pdf_article("pdf-first", allowed_occurrence_ids={"occ-second"})
    with reader.public_snapshot():
        assert [row["occurrence_id"] for row in reader.pdf_article("pdf-second", allowed_occurrence_ids={"occ-second"})["occurrences"]] == ["occ-second"]
        with pytest.raises(RegistryNotFoundError):
            reader.pdf_article("pdf-second", allowed_occurrence_ids={"occ-first"})


def test_two_pdf_parents_of_one_article_have_one_module_and_exact_occurrences(tmp_path):
    from climate_registry.read_api import RegistryReader
    database = _seed(tmp_path); _identity_pdf_sources(database, count=2)
    _merge_identity(database, tmp_path, [("pdf_article", "pdf-first"), ("pdf_article", "pdf-second")])
    reader = _reader(database, tmp_path)
    def check(report):
        assert report["article_count"] == 1 and len(report["articles"]) == 1
        article = report["articles"][0]
        assert article["module_identity"] == {"entity_kind": "article", "entity_id": "good"}
        assert set(article["source_pdf_article_ids"]) == {"pdf-first", "pdf-second"}
        assert sorted(row["occurrence_id"] for row in article["occurrences"]) == ["occ-first", "occ-second"]
    check(reader.pdf_report("f"*64))
    with reader.public_snapshot():
        check(reader.pdf_report("f"*64, page=1, page_size=1))
        assert next(row for row in reader.pdf_reports_all() if row["document_sha256"] == "f"*64)["article_count"] == 1
    paged = reader.pdf_report("f"*64, page=1, page_size=1)
    check(paged); assert paged["pagination"]["total"] == 1
    assert not reader.pdf_report("f"*64, page=2, page_size=1)["articles"]
    assert next(row for row in reader.pdf_reports_all() if row["document_sha256"] == "f"*64)["article_count"] == 1
    assert len(reader.pdf_report_module_catalog("f"*64)) == 1
    output = tmp_path/"application"/"wiki"/"public-registry.json"; output.parent.mkdir(parents=True)
    export_public_snapshot(database, output)
    check(json.loads(output.read_text())["pdf_report_details"]["f"*64])
    with reader.public_snapshot():
        export_public_snapshot(database, tmp_path/"nested-export.json", reader=reader)
    check(json.loads((tmp_path/"nested-export.json").read_text())["pdf_report_details"]["f"*64])
    static = RegistryReader.from_public_export(tmp_path/"application")
    check(static.pdf_report("f"*64, page=1, page_size=1))
    assert next(row for row in static.pdf_reports_all(allowed_occurrence_ids={"occ-first", "occ-second"}) if row["document_sha256"] == "f"*64)["article_count"] == 1


@pytest.mark.parametrize("conflict", ["alias", "occurrence"])
def test_cumulative_ownership_accepts_one_final_root_and_rejects_independent_owners(conflict):
    from climate_registry.identity import approved_aliases, reconcile_approved_views
    def alias(kind, identity, *, selection="entity"):
        return {"source": {"entity_kind": kind, "entity_id": identity, "selection": selection, "occurrence_ids": ["exact-occ"]}}
    def view(*aliases):
        return {"tables": {}, "identity_mapping": {"disposition": "article", "aliases": list(aliases)}}
    snapshots = {("article", "good"): view(alias("article", "old")),
        ("article", "third"): view(alias("article", "good"), alias("article", "old"))}
    assert approved_aliases(snapshots) == {("article", "old"): ("article", "third"), ("article", "good"): ("article", "third")}
    assert set(reconcile_approved_views(deepcopy(snapshots))) == {("article", "third")}
    snapshots[("article", "independent")] = view(alias("article", "old") if conflict == "alias" else alias("pdf_article", "aggregate", selection="occurrences"))
    with pytest.raises(ValueError, match="multiple canonical targets|multiple semantic owners"):
        reconcile_approved_views(snapshots)


def test_locator_survives_later_identity_review_and_ordinary_content_candidate(tmp_path):
    from climate_registry.publication import stage_entities, _approve as approve_fixture
    database = _seed(tmp_path)
    manifest = _manifest(database); manifest["canonical_url"] = "https://example.org/moved-report"
    _approve(database, tmp_path, _ingest(database, tmp_path, manifest))
    _merge_identity(database, tmp_path, [("article", "unchecked")])
    assert _reader(database, tmp_path).article("good")["canonical_url"] == manifest["canonical_url"]
    with sqlite3.connect(database) as db:
        candidates = stage_entities(db, article_ids={"good"})
        assert len(candidates) == 1
        approve_fixture(db, candidates[0], {"basis": "independent exact ordinary native candidate fixture"}, status="accepted_legacy")
    reader = _reader(database, tmp_path)
    assert reader.article("good")["canonical_url"] == manifest["canonical_url"]
    assert reader.article("unchecked")["article_id"] == "good"
    output = tmp_path/"locator-inheritance.json"; export_public_snapshot(database, output)
    assert json.loads(output.read_text())["articles"][0]["canonical_url"] == manifest["canonical_url"]



def test_transferred_content_version_date_survives_unchanged_target_staging(tmp_path):
    import hashlib
    from test_article_content_dates import _manual
    from test_owner_checks import _ingest as ingest_date
    from climate_registry.publication import stage_entities, _approve as approve_fixture
    from climate_registry.owner_dates import applicable_manual_dates
    database = _seed(tmp_path)
    body = "Title good. Summary good. Title unchecked. Summary unchecked. Same climate report landing and download."
    with sqlite3.connect(database) as db:
        for identity in ("good", "unchecked"):
            sha = hashlib.sha256(body.encode()).hexdigest()
            db.execute("INSERT INTO article_content_versions(content_version_id,article_id,content_sha256,markdown_content,markdown_sha256,content_type,extraction_method,extraction_version,first_fetched_at) VALUES(?,?,?,?,?,'text/markdown','fixture','1','2026-10-01')", ("body-"+identity, identity, sha, body, sha))
            db.execute("UPDATE articles SET current_content_version_id=? WHERE article_id=?", ("body-"+identity, identity))
    _check(database, tmp_path, ids={"core:good", "core:unchecked"}, body=body); _approve_all(database)
    authorization = _manual(database, scope="content_version")
    authorization["manual_authorization"]["content_version_id"] = "body-good"
    ingest_date(database, tmp_path, authorization)
    source_decision = _reader(database, tmp_path).article("good")["manual_date_overrides"]["publication_date"]
    _merge_identity(database, tmp_path, [("article", "good")], identity="unchecked")
    assert _reader(database, tmp_path).article("unchecked")["publication_date"] == "2026-10-02"
    with sqlite3.connect(database) as db:
        staged = stage_entities(db, article_ids={"unchecked"})[0]
        candidate = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (staged,)).fetchone()[0])
        assert candidate["derived_display"]["manual_date_overrides"]["publication_date"] == source_decision
        assert candidate["derived_display"]["publication_date"] == "2026-10-02"
        changed = deepcopy(candidate); changed["tables"]["articles"][0]["current_content_version_id"] = "different-body"
        assert not applicable_manual_dates(changed, {"publication_date": source_decision})
        changed = deepcopy(candidate)
        for row in changed["tables"]["article_content_versions"]:
            if row["content_version_id"] == "body-unchecked": row["markdown_content"] += " Different content."
        assert not applicable_manual_dates(changed, {"publication_date": source_decision})
        approve_fixture(db, staged, {"basis": "exact unchanged native fixture"}, status="accepted_legacy")
    assert _reader(database, tmp_path).article("unchecked")["manual_date_overrides"]["publication_date"] == source_decision


def test_partial_pdf_occurrence_mapping_keeps_approved_standalone_remainder(tmp_path):
    from test_registry_publication import _pdf
    from verified_fixtures import seed_verified_articles
    from climate_registry.list_projection import RegistryListProjection
    from climate_registry.read_api import RegistryReader
    database = _seed(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db, identity="shared-pdf", url="https://example.org/shared", occurrence_id="pdf-first")
        row = list(db.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='pdf-first'").fetchone())
        row[0], row[3] = "pdf-second", 3
        raw = json.loads(row[-1]); raw.update(title="Separate material", summary="Independent retained material", occurrence_id="pdf-second", page=3)
        row[-1] = json.dumps(raw)
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)", row)
        seed_verified_articles(db, occurrence_ids={"pdf-first", "pdf-second"})
    _approve_all(database)
    manifest = _manifest(database, [("pdf_article", "shared-pdf")])
    scope = _scope(manifest["plan"]["participants"][1]); scope.update(selection="occurrences", occurrence_ids=["pdf-first"], content_version_id=None, body_sha256=None)
    manifest["aliases"] = [{"source": scope, "same_object": True, "evidence_ids": ["semantic-proof"]}]
    _approve(database, tmp_path, _ingest(database, tmp_path, manifest))
    reader = _reader(database, tmp_path)
    def check(report):
        assert report["article_count"] == len(report["articles"]) == 2
        assert sorted(row["occurrence_id"] for article in report["articles"] for row in article["occurrences"]) == ["pdf-first", "pdf-second"]
    check(reader.pdf_report("f"*64))
    with reader.public_snapshot(): check(reader.pdf_report("f"*64))
    with reader.public_snapshot(direct=True):
        projection = RegistryListProjection(reader)
        try:
            cards = projection.articles(page=1,page_size=20,include_pdf=True)["items"]
            assert {row["article_id"] for row in cards} == {"good", "unchecked", "shared-pdf"}
            standalone = next(row for row in cards if row["article_id"] == "shared-pdf")
            assert standalone["occurrence_count"] == 1
            assert standalone["summary"] == "Independent retained material"
            matching = projection.articles(page=1,page_size=20,include_pdf=True,query="Independent retained material")["items"]
            assert [row["article_id"] for row in matching] == ["shared-pdf"]
        finally: projection.close()
    output = tmp_path/"application"/"wiki"/"public-registry.json"; output.parent.mkdir(parents=True)
    export_public_snapshot(database, output)
    static = RegistryReader.from_public_export(tmp_path/"application")
    check(static.pdf_report("f"*64))
    assert static.approved_merged_article_count() == 3
    for selected, count in (({"pdf-first"}, 1), ({"pdf-second"}, 1), ({"pdf-first", "pdf-second"}, 2)):
        card = next(row for row in static.pdf_reports_all(allowed_occurrence_ids=selected) if row["document_sha256"] == "f"*64)
        assert card["article_count"] == count
        report = static.pdf_report("f"*64, allowed_occurrence_ids=selected, page=1, page_size=5)
        assert report["article_count"] == report["pagination"]["total"] == count
        assert {row["occurrence_id"] for article in report["articles"] for row in article["occurrences"]} == selected


    # A later whole-Article correction keeps this exact partial occurrence ownership.
    _merge_identity(database, tmp_path, [("article", "unchecked")])
    current = _reader(database, tmp_path)
    for direct in (True, False):
        with current.public_snapshot(direct=direct):
            check(current.pdf_report("f"*64))
            assert current.article("unchecked")["article_id"] == "good"
            detail = current.pdf_article("shared-pdf")
            assert {row["occurrence_id"] for row in detail["occurrences"]} == {"pdf-first", "pdf-second"}
            if direct:
                assert detail["canonical_occurrence_mappings"] == {"pdf-first": "good"}
    with current.public_snapshot(direct=True):
        projection = RegistryListProjection(current)
        try:
            assert {row["article_id"] for row in projection.articles(page=1, page_size=10, include_pdf=True)["items"]} == {"good", "shared-pdf"}
            assert [row["article_id"] for row in projection.articles(page=1, page_size=10, include_pdf=True, query="Independent retained material")["items"]] == ["shared-pdf"]
        finally:
            projection.close()
    export_public_snapshot(database, output)
    combined = RegistryReader.from_public_export(tmp_path/"application")
    check(combined.pdf_report("f"*64))
    assert combined.article("unchecked")["article_id"] == "good"
    assert combined.approved_merged_article_count() == 2


@pytest.mark.parametrize("selection",[None,"entity","occurrences","multiple"])
def test_pdf_detail_keeps_partial_parent_ownership_exact_across_public_consumers(tmp_path,monkeypatch,selection):
    from contextlib import nullcontext
    from fastapi.testclient import TestClient
    from climate_registry.display import DisplayReader
    from climate_registry.list_projection import group_pdf_articles
    from climate_registry.read_api import RegistryReader
    from test_registry_publication import _pdf
    from verified_fixtures import seed_verified_articles
    import api_server
    database=_seed(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="shared-pdf",url="https://example.org/shared",occurrence_id="pdf-first")
        second=list(db.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='pdf-first'").fetchone())
        second[0],second[3]="pdf-second",3
        raw=json.loads(second[-1]);raw.update(title="Separate material",summary="Independent retained material",occurrence_id="pdf-second",page=3)
        second[-1]=json.dumps(raw)
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",second)
        seed_verified_articles(db,occurrence_ids={"pdf-first","pdf-second"})
    _approve_all(database)
    if selection:
        manifest=_manifest(database,[("pdf_article","shared-pdf")])
        scope=_scope(manifest["plan"]["participants"][1])
        if selection in {"occurrences","multiple"}:scope.update(selection="occurrences",occurrence_ids=["pdf-first"],content_version_id=None,body_sha256=None)
        manifest["aliases"]=[{"source":scope,"same_object":True,"evidence_ids":["semantic-proof"]}]
        staged=_ingest(database,tmp_path,manifest)
        assert not _reader(database,tmp_path).pdf_article("shared-pdf").get("canonical_occurrence_mappings")
        _approve(database,tmp_path,staged)
        if selection == "multiple":
            manifest=_manifest(database,[("pdf_article","shared-pdf")],identity="unchecked")
            scope=_scope(manifest["plan"]["participants"][1]);scope.update(selection="occurrences",occurrence_ids=["pdf-second"],content_version_id=None,body_sha256=None)
            manifest["aliases"]=[{"source":scope,"same_object":True,"evidence_ids":["semantic-proof"]}]
            staged=_ingest(database,tmp_path,manifest)
            assert _reader(database,tmp_path).pdf_article("shared-pdf")["canonical_occurrence_mappings"] == {"pdf-first":"good"}
            _approve(database,tmp_path,staged)
    with sqlite3.connect(database) as db:
        originals={table:db.execute("SELECT * FROM "+table).fetchall() for table in (
            "pdf_intake_articles","pdf_intake_article_occurrences","registry_reviews")}
        retained_parent=db.execute("SELECT latest_candidate_sha256,published_candidate_sha256 FROM registry_publication WHERE entity_kind='pdf_article' AND entity_id='shared-pdf'").fetchone()
        owner="good" if selection == "entity" else "shared-pdf"
        kind="article" if selection == "entity" else "pdf_article"
        views=published_snapshots(db,("article","pdf_article"))
        if selection == "multiple":
            assert ("pdf_article","shared-pdf") not in views
            assert retained_parent[1]
            approved=None
        else:
            approved=views[(kind,owner)]["_approved_candidate_sha256"]
        owner_references={identity:{"entity_kind":"article","entity_id":identity,
            "candidate_sha256":view["_approved_candidate_sha256"],"version_id":next(row.get("current_version_id")
                for row in view["tables"]["articles"] if row["article_id"] == identity)}
            for (entity,identity),view in views.items() if entity == "article"}
    mappings=({"pdf-first":"good","pdf-second":"good"} if selection == "entity"
        else {"pdf-first":"good","pdf-second":"unchecked"} if selection == "multiple"
        else {"pdf-first":"good"} if selection == "occurrences" else {})
    expected=({"article:good":{"pdf-first","pdf-second"}} if selection == "entity"
        else {"article:good":{"pdf-first"},"article:unchecked":{"pdf-second"}} if selection == "multiple"
        else {"article:good":{"pdf-first"},"pdf_article:shared-pdf":{"pdf-second"}} if selection == "occurrences"
        else {"pdf_article:shared-pdf":{"pdf-first","pdf-second"}})
    def check(item,current):
        assert {row["occurrence_id"] for row in item["occurrences"]} == {"pdf-first","pdf-second"}
        assert item.get("canonical_occurrence_mappings",{}) == mappings
        assert item.get("core_article_id") == ("good" if selection == "entity" else None)
        assert item.get("canonical_display_article_id") in ({None,"good"} if selection == "entity" else {None})
        reference=None if selection == "multiple" else {"entity_kind":kind,"entity_id":owner,"candidate_sha256":approved,"version_id":None}
        assert item["record_reference"] == reference
        assert item["published_candidate_sha256"] == approved
        for canonical in set(mappings.values()):
            detail=current.article(canonical)
            if current.static_snapshot is None:
                assert detail["record_reference"] == owner_references[canonical]
            else:
                assert "record_reference" not in detail
            assert detail["article_id"] == canonical
            assert detail.get("current_version_id") == owner_references[canonical]["version_id"]
            assert detail["published_candidate_sha256"] == owner_references[canonical]["candidate_sha256"]
        assert {key:{row["occurrence_id"] for row in value["occurrences"]} for key,value in group_pdf_articles([item]).items()} == expected
        assert {(row["occurrence_id"],row["source_document_sha256"],row["page"]) for row in item["occurrences"]} == {
            ("pdf-first","f"*64,2),("pdf-second","f"*64,3)}
    def check_owner_http(canonical,current):
        target=TestClient(api_server.app).get("/api/registry/articles/"+canonical)
        assert target.status_code == 200,target.text
        actual=target.json();expected=current.article(canonical)
        assert "record_reference" not in actual
        assert actual["article_id"] == canonical
        assert actual["published_candidate_sha256"] == owner_references[canonical]["candidate_sha256"]
        for field in ("canonical_url","current_version_id","content_version_id","content"):
            assert actual.get(field) == expected.get(field)
    reader=_reader(database,tmp_path)
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):check(reader.pdf_article("shared-pdf"),reader)
    with sqlite3.connect(database) as db:check(DisplayReader(db).article("shared-pdf",pdf=True),reader)
    monkeypatch.setattr(api_server,"_registry_reader",lambda:reader)
    monkeypatch.setattr(api_server,"_pdf_registry_view",lambda:(reader,None,None))
    monkeypatch.setattr(api_server,"_range_report_overlay",lambda:(None,None,None))
    for direct in (False,True):
        with monkeypatch.context() as patch:
            if direct:patch.setattr(api_server,"display_snapshot",lambda selected:nullcontext(None))
            response=TestClient(api_server.app).get("/api/registry/pdf-intake/articles/shared-pdf")
            assert response.status_code == 200,response.text
            check(response.json(),reader)
            for canonical in set(mappings.values()):
                check_owner_http(canonical,reader)
    root=tmp_path/"partial-detail-static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json")
    static=RegistryReader.from_public_export(root);check(static.pdf_article("shared-pdf"),static)
    monkeypatch.setattr(api_server,"_registry_reader",lambda:static)
    monkeypatch.setattr(api_server,"_pdf_registry_view",lambda:(static,None,None))
    response=TestClient(api_server.app).get("/api/registry/pdf-intake/articles/shared-pdf")
    assert response.status_code == 200,response.text
    check(response.json(),static)
    with sqlite3.connect(database) as db:
        for table,rows in originals.items():assert db.execute("SELECT * FROM "+table).fetchall() == rows
        assert db.execute("SELECT latest_candidate_sha256,published_candidate_sha256 FROM registry_publication WHERE entity_kind='pdf_article' AND entity_id='shared-pdf'").fetchone() == retained_parent


@pytest.mark.parametrize("mappings",[
    {"first":"good","second":"good"}, {"first":"good"},
    {"first":"good","second":"other"}, {"stale":"good"}, {},
])
def test_pdf_parent_ownership_projection_preserves_exact_scope(mappings):
    from climate_registry.read_api import _pdf_parent_ownership,_merge_pdf_parent_payloads
    from climate_registry.list_projection import group_pdf_articles
    # Projection-only DTO control; publication authority is exercised above.
    payload={"article_id":"parent","core_article_id":"good","canonical_display_article_id":"good",
        "published_candidate_sha256":"a"*64,"first_seen":"2026-01-01","last_seen":"2026-01-02",
        "record_reference":{"entity_kind":"article","entity_id":"good","candidate_sha256":"a"*64,"version_id":None},
        "occurrences":[{"occurrence_id":"first","source_document_sha256":"d"*64,"page":1},
            {"occurrence_id":"second","source_document_sha256":"d"*64,"page":2}]}
    payload["canonical_occurrence_mappings"]=mappings
    whole=mappings == {"first":"good","second":"good"}
    actual=_pdf_parent_ownership(deepcopy(payload))
    if mappings == {"stale":"good"}:
        previous=deepcopy(payload);previous["occurrences"]=previous["occurrences"][:1]
        current={**payload,"core_article_id":None,"canonical_display_article_id":None,
            "occurrences":payload["occurrences"][1:],"canonical_occurrence_mappings":{},
            "published_candidate_sha256":"b"*64,"record_reference":{"entity_kind":"pdf_article",
                "entity_id":"parent","candidate_sha256":"b"*64,"version_id":None}}
        combined=_merge_pdf_parent_payloads(previous,current)
        assert combined.get("core_article_id") is None and combined.get("canonical_display_article_id") is None
        assert combined["canonical_occurrence_mappings"] == {}
        assert combined["record_reference"] == current["record_reference"]
        assert {row["occurrence_id"] for row in combined["occurrences"]} == {"first","second"}
    assert actual["occurrences"] == payload["occurrences"]
    assert actual.get("core_article_id") == ("good" if whole else None)
    assert actual.get("canonical_display_article_id") == ("good" if whole else None)
    assert actual["record_reference"] == (payload["record_reference"] if whole else None)
    assert actual["published_candidate_sha256"] == ("a"*64 if whole else None)
    groups=group_pdf_articles([actual])
    for key,value in groups.items():
        for occurrence in value["occurrences"]:
            owner=mappings.get(occurrence["occurrence_id"])
            assert key == ("article:"+owner if owner else "pdf_article:parent")


def test_canonical_article_merge_keeps_alias_report_appearances_public(tmp_path):
    database = _seed(tmp_path)
    with sqlite3.connect(database) as db:
        for ordinal, (identity, day) in enumerate((("good", "2026-09-07"), ("unchecked", "2026-09-14")), 1):
            report = "report-"+identity; discovery = "discovery-"+identity
            db.execute("INSERT INTO reports(report_id,report_date,filename,report_title,report_sha256,cadence,report_format,parse_warnings_json) VALUES(?,?,?,?,?,'weekly','weekly-pillars-v1','[]')", (report,day,"climate-monitor-"+day+".md","Weekly report",str(ordinal)*64))
            db.execute("INSERT INTO discoveries(discovery_id,report_id,ordinal,section,pillar,article_id,version_id,raw_url,observed_title,observed_summary,selected) VALUES(?,?,1,'pillar-a','A',?,?,?,?,?,1)", (discovery,report,identity,"v-"+identity,"https://example.org/"+identity,"Title "+identity,"Summary "+identity))
            db.execute("INSERT INTO report_appearances(report_id,article_id,version_id,discovery_id,section,pillar,ordinal,disposition) VALUES(?,?,?,?,'pillar-a','A',1,'new')", (report,identity,"v-"+identity,discovery))
    _check(database, tmp_path, ids={"core:good", "core:unchecked"}, body="Title good. Summary good. Title unchecked. Summary unchecked. Same climate report landing and download."); _approve_all(database)
    with sqlite3.connect(database) as db:
        originals = {table: db.execute("SELECT * FROM "+table).fetchall() for table in ("discoveries", "report_appearances", "article_versions")}
    _merge_identity(database, tmp_path, [("article", "unchecked")])
    reader = _reader(database, tmp_path)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            assert {row["report_date"]:row["article_count"] for row in reader.reports()["items"]} == {"2026-09-07":1,"2026-09-14":1}
            detail = reader.article("good")
            assert {row["report_date"] for row in detail["appearances"]} == {"2026-09-07","2026-09-14"}
            report = reader.report("2026-09-14")
            assert len(report["articles"]) == 1 and report["articles"][0]["article_id"] == "good"
            assert report["articles"][0]["report_summary"] == "Summary unchecked"
    output=tmp_path/"provenance.json";export_public_snapshot(database,output)
    assert json.loads(output.read_text())["registry_report_details"]["2026-09-14"]["payload"]["articles"][0]["article_id"] == "good"
    with sqlite3.connect(database) as db:
        for table, rows in originals.items(): assert db.execute("SELECT * FROM "+table).fetchall() == rows


def test_acquisition_resume_retains_requested_locator_after_approved_move(tmp_path):
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    from test_issue112_acquisition import _item, _batch, NOW
    database = _seed(tmp_path, acquisition_source=True)
    manifest = _manifest(database); manifest["canonical_url"] = "https://example.org/moved-report"
    _approve(database, tmp_path, _ingest(database, tmp_path, manifest))
    item = _item(manifest["canonical_url"], discovery_kind="site", discovery_ref="moved", discovery_search_ref=None)
    batch = _batch([item], completed=False, searches=[], search_decision={"status":"no_search","reason":"site-only"})
    store_acquisition_batch(database,batch)
    with sqlite3.connect(database) as db:
        before = db.execute("SELECT * FROM article_fetches").fetchall()
    batch["completed_at"] = NOW
    store_acquisition_batch(database,batch)
    loaded=load_acquisition_batch(database,"batch-112")
    assert loaded["completed_at"] == NOW and loaded["items"][0]["article_id"] == "good"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM article_fetches").fetchall() == before
        assert db.execute("SELECT canonical_url FROM articles WHERE article_id='good'").fetchone()[0] == "https://example.org/good"



def test_approved_dates_alias_merges_current_identity_and_retains_source_occurrences(tmp_path, monkeypatch):
    from test_verified_publication import _pdf_pair
    from climate_registry.read_api import RegistryReader
    database = _seed(tmp_path)
    event = _pdf_pair(database, verified_source=True)
    _check(database, tmp_path, kind="meetings", ids={"target-occ", "source-occ"}, candidate=event)
    _approve_all(database)
    with sqlite3.connect(database) as db:
        originals = db.execute("SELECT * FROM pdf_intake_calendar_items ORDER BY occurrence_id").fetchall()
    manifest = plan_owner_check(database, "pdf_meeting", "target", identity_correction=True,
        identity_participants=[("pdf_meeting", "source")])
    manifest.update(actor={"id": "identity-author", "task_id": "identity-author-task"},
        authorization_reference="actual owner same-edition correction", audit_reference="retained audit/identity",
        checked_at="2026-10-25T18:00:00Z")
    manifest["evidence"] = [{"id": "same-edition", "source": _scope(manifest["plan"]["participants"][0]),
        "text": event["name"], "excerpt": "2027", "reason": "Both observations identify this same conference edition."}]
    manifest["aliases"] = [{"source": _scope(manifest["plan"]["participants"][1]),
        "same_object": True, "evidence_ids": ["same-edition"]}]
    staged = _ingest(database, tmp_path, manifest)
    assert len(_reader(database, tmp_path).meetings_all(base_date="1900-01-01")) == 2
    _approve(database, tmp_path, staged)

    def check(current):
        events = current.meetings_all(base_date="1900-01-01")
        assert len(events) == 1 and events[0]["event_id"] == "target"
        observations = events[0]["pdf_observations"]
        assert {row["occurrence_id"] for row in observations} == {"target-occ", "source-occ"}
        assert {row["event_id"] for row in observations} == {"source", "target"}
        assert len(observations) == 2
        assert events[0]["start_date"] == "2027-06-10"
        assert events[0]["end_date"] == "2027-06-12"
        source = current.pdf_report("a"*64)["calendar_items"][0]
        assert source["event_id"] == "source" and source["canonical_pdf_event_id"] == "target"
        wrong_document = {**source, "source_document_sha256": "0"*64}
        wrong_occurrence = {**source, "occurrence_id": "outside-approved-scope"}
        assert all(not row.get("canonical_pdf_event_id") for row in current.resolve_pdf_meeting_identities([wrong_document, wrong_occurrence]))
    reader = _reader(database, tmp_path)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            check(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    output = root/"wiki/public-registry.json"
    export_public_snapshot(database, output)
    artifact = json.loads(output.read_text())
    assert [row["event_id"] for row in artifact["meeting_records"]] == ["target"]
    assert {row["event_id"] for row in artifact["meetings"]} == {"source", "target"}
    check(RegistryReader.from_public_export(root))
    from climate_registry.publication import install_git_snapshot
    from climate_registry.schema import apply_migrations
    installed = tmp_path/"installed.sqlite3"
    with sqlite3.connect(installed) as db:
        apply_migrations(db)
    db.close()
    install_git_snapshot(installed, root/"wiki")
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", "1")
    check(RegistryReader(installed, repository_root=root))
    with sqlite3.connect(database) as db:
        assert list(published_snapshots(db, ("pdf_meeting",))) == [("pdf_meeting", "target")]
        assert db.execute("SELECT * FROM pdf_intake_calendar_items ORDER BY occurrence_id").fetchall() == originals


@pytest.mark.parametrize("same_report", [False, True])
def test_canonical_report_wiki_resolves_old_appearance_and_renders_article_once(tmp_path, monkeypatch, same_report):
    from climate_registry.publication import public_wiki_pages
    from climate_registry.read_api import RegistryReader
    from climate_registry.wiki import render_registry_page
    database = _seed(tmp_path)
    day = "2026-09-14"
    with sqlite3.connect(database) as db:
        for ordinal, identity in enumerate(("good", "unchecked"), 1):
            report_day = day if same_report or identity == "unchecked" else "2026-09-07"
            report = "report-"+report_day; discovery = "discovery-"+identity
            db.execute("INSERT OR IGNORE INTO reports(report_id,report_date,filename,report_title,report_sha256,cadence,report_format,parse_warnings_json) VALUES(?,?,?,?,?,'weekly','weekly-pillars-v1','[]')", (report,report_day,"climate-monitor-"+report_day+".md","Weekly report",str(ordinal)*64))
            db.execute("INSERT INTO discoveries(discovery_id,report_id,ordinal,section,pillar,article_id,version_id,raw_url,observed_title,observed_summary,selected) VALUES(?,?,?,'pillar-a','A',?,?,?,?,?,1)", (discovery,report,ordinal,identity,"v-"+identity,"https://example.org/"+identity,"Title "+identity,"Summary "+identity))
            db.execute("INSERT INTO report_appearances(report_id,article_id,version_id,discovery_id,section,pillar,ordinal,disposition) VALUES(?,?,?,?,'pillar-a','A',?,'new')", (report,identity,"v-"+identity,discovery,ordinal))
    _check(database, tmp_path, ids={"core:good", "core:unchecked"}, body="Title good. Summary good. Title unchecked. Summary unchecked. Same climate report landing and download.")
    _approve_all(database)
    with sqlite3.connect(database) as db:
        originals = {table: db.execute("SELECT * FROM "+table).fetchall() for table in ("reports", "discoveries", "report_appearances", "article_versions")}
    _merge_identity(database, tmp_path, [("article", "unchecked")])
    page = "climate-monitor-"+day+".md"
    reader = _reader(database, tmp_path)
    def check(markdown):
        assert markdown is not None
        assert markdown.count("## Title good") == 1
        assert markdown.count("Summary good") == 1
        assert "Source report: "+page in markdown
    def no_article_n_plus_one(*args, **kwargs):
        pytest.fail("canonical report page must select approved snapshots in one batch")
    # Single page must resolve aliases in one batch before the bulk path is used.
    with monkeypatch.context() as patch:
        patch.setattr(RegistryReader, "article", no_article_n_plus_one)
        check(public_wiki_pages(database, page=page, reader=reader).get(page))
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            with monkeypatch.context() as patch:
                patch.setattr(RegistryReader, "article", no_article_n_plus_one)
                check(render_registry_page(database, page, reader=reader))
            check(public_wiki_pages(database, reader=reader)[page])
            assert [row["article_id"] for row in reader.report(day)["articles"]] == ["good"]
            assert reader.article("unchecked")["article_id"] == "good"
            if direct:
                from climate_registry.list_projection import RegistryListProjection
                projection = RegistryListProjection(reader)
                try:
                    assert [row["article_id"] for row in projection.articles(page=1, page_size=10)["items"]] == ["good"]
                finally:
                    projection.close()
            appearances = reader.article("good")["appearances"]
            assert {row["version_id"] for row in appearances} == {"v-good", "v-unchecked"}
            assert {row["original_url"] for row in appearances} == {"https://example.org/good", "https://example.org/unchecked"}
    root=tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    output=root/"wiki/public-registry.json"; export_public_snapshot(database,output)
    artifact=json.loads(output.read_text())
    check(artifact["wiki_pages"][page])
    assert len(artifact["registry_report_details"][day]["payload"]["articles"]) == 1
    static=RegistryReader.from_public_export(root)
    assert static.article("unchecked")["article_id"] == "good"
    from climate_registry.publication import install_git_snapshot
    from climate_registry.schema import apply_migrations
    installed = tmp_path/"installed.sqlite3"
    with sqlite3.connect(installed) as db:
        apply_migrations(db)
    db.close()
    install_git_snapshot(installed, root/"wiki")
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", "1")
    installed_reader = RegistryReader(installed, repository_root=root)
    assert [row["article_id"] for row in installed_reader.report(day)["articles"]] == ["good"]
    assert installed_reader.article("unchecked")["article_id"] == "good"
    check(public_wiki_pages(installed, wiki_dir=root/"wiki", page=page)[page])
    check(public_wiki_pages(database, wiki_dir=root/"wiki", page=page)[page])
    assert {row["version_id"] for row in static.article("good")["appearances"]} == {"v-good", "v-unchecked"}
    with sqlite3.connect(database) as db:
        for table, rows in originals.items():
            assert db.execute("SELECT * FROM "+table).fetchall() == rows



def test_native_dates_mapping_consumes_exact_pdf_occurrence_across_modes(tmp_path, monkeypatch):
    from test_issue136_meetings import _database as native_database, _candidate
    from test_verified_publication import _pdf_pair, DOC
    from climate_monitor.meetings import process_batch
    from verified_fixtures import seed_verified_articles, seed_verified_native_meetings
    from climate_registry.publication import stage_entities, _approve as fixture_approve, install_git_snapshot
    from climate_registry.read_api import RegistryReader
    from climate_registry.schema import apply_migrations
    body = "IAIS annual conference for 2027. June 10–12, 2027. Climate risk agenda."
    database = native_database(tmp_path, [body])
    candidate = _candidate(name="IAIS annual conference for 2027", event_type="conference",
        organizer=None, location=None, online_url=None, raw_time_text=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None)
    assert process_batch(database, "batch", prompt_text="fixture", prompt_version="v1",
        provider="fixture", model="fixture", extractor=lambda request: {"events": [candidate]})["status"] == "succeeded"
    with sqlite3.connect(database) as db:
        native = db.execute("SELECT event_id FROM climate_events").fetchone()[0]
        db.execute("INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at) VALUES(?, '/evidence/report.pdf','report.pdf','application/pdf',4,?,'{}','2026-09-14')", (DOC, "e"*64))
    event = _pdf_pair(database)
    _check(database, tmp_path, kind="meetings", ids={"target-occ"}, candidate=event)
    with sqlite3.connect(database) as db:
        seed_verified_articles(db); seed_verified_native_meetings(db)
        for sha in stage_entities(db):
            fixture_approve(db, sha, {"basis": "isolated exact fixture"}, status="accepted_legacy")
        original = db.execute("SELECT * FROM pdf_intake_calendar_items ORDER BY occurrence_id").fetchall()
    manifest = plan_owner_check(database, "meeting", native, identity_correction=True,
        identity_participants=[("pdf_meeting", "target")])
    manifest.update(actor={"id":"identity-author", "task_id":"identity-author-task"},
        authorization_reference="owner same edition mapping", audit_reference="audit/native-pdf-map",
        checked_at="2026-10-25T18:00:00Z", evidence=[{"id":"proof", "source":_scope(manifest["plan"]["participants"][0]),
            "text":body, "excerpt":"IAIS annual conference for 2027", "reason":"Retained sources identify this same event edition."}])
    manifest["aliases"] = [{"source":_scope(manifest["plan"]["participants"][1]), "same_object":True, "evidence_ids":["proof"]}]
    staged = _ingest(database, tmp_path, manifest)
    assert len(_reader(database, tmp_path).meetings_all(base_date="1900-01-01")) == 2
    _approve(database, tmp_path, staged)
    def check(current):
        rows = current.meetings_all(base_date="1900-01-01")
        assert len(rows) == 1 and rows[0]["event_id"] == native
        assert rows[0]["name"] == candidate["name"]
        assert [row["occurrence_id"] for row in rows[0]["pdf_observations"]] == ["target-occ"]
        source = current.pdf_report(DOC)["calendar_items"][0]
        assert source["event_id"] == "target" and source["canonical_event_id"] == native
        for changed in ({"source_document_sha256":"0"*64}, {"occurrence_id":"outside-scope"}):
            resolved = current.resolve_pdf_meeting_identities([{**source, **changed}])[0]
            assert not resolved.get("canonical_event_id") and not resolved.get("canonical_pdf_event_id")
    reader = _reader(database, tmp_path)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct): check(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    artifact = json.loads((root/"wiki/public-registry.json").read_text())
    assert [row["event_id"] for row in artifact["meeting_records"]] == [native]
    check(RegistryReader.from_public_export(root))
    installed = tmp_path/"installed.sqlite3"
    with sqlite3.connect(installed) as db: apply_migrations(db)
    db.close(); install_git_snapshot(installed, root/"wiki")
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", "1")
    check(RegistryReader(installed, repository_root=root))
    with sqlite3.connect(database) as db:
        assert list(published_snapshots(db, ("meeting","pdf_meeting"))) == [("meeting",native)]
        assert db.execute("SELECT * FROM pdf_intake_calendar_items ORDER BY occurrence_id").fetchall() == original


@pytest.mark.parametrize("same_report", [False, True])
def test_source_wiki_keeps_same_url_materials_bound_to_exact_report_appearances(tmp_path, same_report):
    import hashlib
    from climate_registry.publication import public_wiki_pages
    from climate_registry.read_api import RegistryReader
    database = _seed(tmp_path)
    sources = tmp_path/"sources"; sources.mkdir()
    groups = {"2026-09-07": ["good", "unchecked"]} if same_report else {"2026-09-07":["good"], "2026-09-14":["unchecked"]}
    def report_text(day, identities):
        return ("# Weekly Climate Monitor\n**Report Date:** "+day+"\n## Executive Summary\n- Sites checked: **1**, succeeded: **1**, failed: **0**\n## Pillar A — Changes\n"+
            "".join("- **Title "+identity+"**\n  - Summary "+identity+"\n  🔗 https://example.org/shared\n" for identity in identities)+
            "## Pillar B — Intelligence\n## Original Links\n- https://example.org/shared\n")
    with sqlite3.connect(database) as db:
        db.execute("UPDATE articles SET canonical_url='https://example.org/shared'")
        for day, identities in groups.items():
            filename = "climate-monitor-"+day+".md"; report="report-"+day
            (sources/filename).write_text(report_text(day,identities),encoding="utf-8")
            sha=hashlib.sha256((sources/filename).read_bytes()).hexdigest()
            db.execute("INSERT INTO reports(report_id,report_date,filename,report_title,report_sha256,cadence,report_format,parse_warnings_json) VALUES(?,?,?,?,?,'weekly','weekly-pillars-v1','[]')",(report,day,filename,"Weekly",sha))
            for ordinal, identity in enumerate(identities,1):
                discovery="discovery-"+identity
                db.execute("INSERT INTO discoveries(discovery_id,report_id,ordinal,section,pillar,article_id,version_id,raw_url,observed_title,observed_summary,selected) VALUES(?,?,?,'pillar-a','A',?,?,'https://example.org/shared',?,?,1)",(discovery,report,ordinal,identity,"v-"+identity,"Title "+identity,"Summary "+identity))
                db.execute("INSERT INTO report_appearances(report_id,article_id,version_id,discovery_id,section,pillar,ordinal,disposition) VALUES(?,?,?,?,'pillar-a','A',?,'new')",(report,identity,"v-"+identity,discovery,ordinal))
    # URL-only archive reuse must fail closed while the bound reports still render.
    (sources/"climate-monitor-2026-09-21.md").write_text(report_text("2026-09-21",["unbound"]),encoding="utf-8")
    _check(database,tmp_path,ids={"core:good","core:unchecked"},body="Title good. Summary good. Title unchecked. Summary unchecked. Same climate report landing and download.")
    _approve_all(database)
    reader=RegistryReader(database,repository_root=tmp_path/"app",source_dir=sources)
    with sqlite3.connect(database) as db:
        original={table:db.execute("SELECT * FROM "+table).fetchall() for table in ("reports","discoveries","report_appearances","article_versions")}
    def check(pages):
        for day, identities in groups.items():
            page=pages["climate-monitor-"+day+".md"]
            assert [line for line in page.splitlines() if line.startswith("## Title ")] == ["## Title "+identity for identity in identities]
            for identity in identities: assert page.count("Summary "+identity) == 1
        assert "climate-monitor-2026-09-21.md" not in pages
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):
            check(public_wiki_pages(database,reader=reader,source_dir=sources))
            for day in groups:
                page="climate-monitor-"+day+".md"
                check({**public_wiki_pages(database,reader=reader,source_dir=sources),**public_wiki_pages(database,reader=reader,source_dir=sources,page=page)})
    root=tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    output=root/"wiki/public-registry.json";export_public_snapshot(database,output,reader=reader,source_dir=sources)
    artifact=json.loads(output.read_text());check(artifact["wiki_pages"])
    static=RegistryReader.from_public_export(root)
    assert {row["article_id"] for row in static.articles()["items"]} == {"good","unchecked"}
    with sqlite3.connect(database) as db:
        for table, rows in original.items(): assert db.execute("SELECT * FROM "+table).fetchall() == rows


def test_occurrence_only_article_scope_does_not_redirect_static_source_identity(tmp_path, monkeypatch):
    from test_article_content_dates import _manual
    from test_owner_checks import _ingest as ingest_date
    from climate_registry.read_api import RegistryReader
    from climate_registry.publication import install_git_snapshot
    from climate_registry.schema import apply_migrations
    database=_seed(tmp_path);_identity_pdf_sources(database)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE pdf_intake_articles SET core_article_id='good',confirmation_basis='exact_url_eligible_detail' WHERE article_id='pdf-first'")
    _approve_all(database); ingest_date(database,tmp_path,_manual(database))
    manifest=_manifest(database,[("article","good")],identity="unchecked")
    scope=_scope(manifest["plan"]["participants"][1]);scope.update(selection="occurrences",occurrence_ids=["occ-first"],content_version_id=None,body_sha256=None)
    manifest["aliases"]=[{"source":scope,"same_object":True,"evidence_ids":["semantic-proof"]}]
    _approve(database,tmp_path,_ingest(database,tmp_path,manifest))
    def check(current):
        assert {row["article_id"] for row in current.articles()["items"]} == {"good","unchecked"}
        good=current.article("good"); target=current.article("unchecked")
        assert good["article_id"] == "good" and target["article_id"] == "unchecked"
        assert good["publication_date"] == "2026-10-02" and good["manual_date_overrides"]
        assert not target.get("manual_date_overrides")
        assert not good.get("pdf_occurrences")
        assert [row["occurrence_id"] for row in target["pdf_occurrences"]] == ["occ-first"]
    reader=_reader(database,tmp_path)
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):check(reader)
    root=tmp_path/"static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json");check(RegistryReader.from_public_export(root))
    installed=tmp_path/"installed.sqlite3"
    with sqlite3.connect(installed) as db:apply_migrations(db)
    db.close();install_git_snapshot(installed,root/"wiki")
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1");check(RegistryReader(installed,repository_root=root))



def _write_followup_identity_report(source_dir, url="https://example.org/moved-report"):
    source_dir.mkdir()
    path=source_dir/"climate-monitor-2026-10-26.md"
    path.write_text("# Weekly Climate Monitor\n**Report Date:** 2026-10-26\n## Executive Summary\n- Sites checked: **1**, succeeded: **1**, failed: **0**\n## Pillar A — Changes\n- **Title good**\n  - Summary good\n  🔗 "+url+"\n## Pillar B — Intelligence\n## Original Links\n- "+url+"\n",encoding="utf-8")
    return path


@pytest.mark.parametrize("approved", [False, True])
def test_report_writer_uses_only_approved_locator_mapping_and_retains_occurrence(tmp_path, approved):
    from climate_registry.persistent import update_registry
    from climate_registry.acquisition import _article_identity, store_acquisition_batch, load_acquisition_batch
    from climate_registry.audit import _stable_id
    from climate_registry.read_api import RegistryReader
    from test_issue112_acquisition import _item, _batch
    database=_seed(tmp_path,acquisition_source=True)
    manifest=_manifest(database);manifest["canonical_url"]="https://example.org/moved-report"
    staged=_ingest(database,tmp_path,manifest)
    if approved:_approve(database,tmp_path,staged)
    path=_write_followup_identity_report(tmp_path/"sources")
    with sqlite3.connect(database) as db:
        original_versions=db.execute("SELECT * FROM article_versions").fetchall()
        original_candidates=db.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates").fetchall()
    assert update_registry(path.parent,database,tmp_path/"report-backups")["status"] == "updated"
    expected="good" if approved else _stable_id("article",manifest["canonical_url"])
    with sqlite3.connect(database) as db:
        rows=db.execute("SELECT ra.article_id,ra.version_id,d.article_id,d.raw_url,d.version_id FROM report_appearances ra JOIN discoveries d USING(discovery_id)").fetchall()
        assert len(rows)==1 and rows[0][0]==rows[0][2]==expected and rows[0][1]==rows[0][4]
        assert rows[0][3]==manifest["canonical_url"]
        assert db.execute("SELECT canonical_url FROM articles WHERE article_id='good'").fetchone()[0]=="https://example.org/good"
        assert db.execute("SELECT count(*) FROM articles").fetchone()[0]==(2 if approved else 3)
        assert _article_identity(db,{"canonical_url":manifest["canonical_url"]})==expected
        for row in original_versions:assert db.execute("SELECT * FROM article_versions WHERE version_id=?",(row[0],)).fetchone()==row
        for sha,encoded in original_candidates:assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(sha,)).fetchone()[0]==encoded
    reader=_reader(database,tmp_path)
    assert reader.article("good")["canonical_url"]==(manifest["canonical_url"] if approved else "https://example.org/good")
    if not approved:return
    # A subsequent real acquisition at the approved locator reuses the same owner.
    body="Title good. Summary good. Same climate report landing and download."
    item=_item(manifest["canonical_url"],discovery_kind="site",discovery_ref="followup",discovery_search_ref=None)
    item["evidence"].update(content=body,content_hash=__import__("hashlib").sha256(body.encode()).hexdigest())
    store_acquisition_batch(database,_batch([item],searches=[],search_decision={"status":"no_search","reason":"site-only"}))
    assert load_acquisition_batch(database,"batch-112")["items"][0]["article_id"]=="good"
    _check(database,tmp_path,ids={"core:good"},body=body);_approve_all(database)
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):
            assert {row["article_id"] for row in reader.articles()["items"]}=={"good","unchecked"}
            assert [row["article_id"] for row in reader.report("2026-10-26")["articles"]]==["good"]
            assert reader.article("good")["appearances"][0]["original_url"]==manifest["canonical_url"]
    root=tmp_path/"static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json")
    static=RegistryReader.from_public_export(root)
    assert {row["article_id"] for row in static.articles()["items"]}=={"good","unchecked"}
    assert [row["article_id"] for row in static.report("2026-10-26")["articles"]]==["good"]


def test_report_writer_holds_ambiguous_url_without_hiding_distinct_materials(tmp_path):
    from climate_registry.persistent import update_registry
    from climate_registry.errors import RegistryBuildError
    database=_seed(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE articles SET canonical_url='https://example.org/shared'")
    _check(database,tmp_path,ids={"core:good","core:unchecked"},body="Title good. Summary good. Title unchecked. Summary unchecked. Same climate report landing and download.")
    _approve_all(database)
    path=_write_followup_identity_report(tmp_path/"sources","https://example.org/shared")
    before=database.read_bytes()
    with pytest.raises(RegistryBuildError,match="ambiguous URL"):
        update_registry(path.parent,database,tmp_path/"report-backups")
    assert database.read_bytes()==before
    assert {row["article_id"] for row in _reader(database,tmp_path).articles()["items"]}=={"good","unchecked"}


@pytest.mark.parametrize("manual", [False, True])
def test_activity_only_preserves_exact_approved_linked_article_classification(tmp_path, manual):
    from test_verified_publication import _pdf_pair
    from test_owner_activity_dates import _human_activity, _manifest as source_activity
    from test_owner_publication_dates import _date_review, _approve as approve_date
    from climate_registry.list_projection import RegistryListProjection
    from climate_registry.publication import snapshot_entity, stage_snapshot, _approve as fixture_approve
    from climate_registry.read_api import RegistryReader
    database=_seed(tmp_path)
    body="Title good. Summary good. Same climate report landing and download. Conference 2027 announcement analysis."
    sha=__import__("hashlib").sha256(body.encode()).hexdigest()
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_content_versions VALUES('body-good','good',?,?,?,'text/html',NULL,'fixture','v1','2026-10-01T00:00:00Z')",(sha,body,sha))
        db.execute("UPDATE articles SET current_content_version_id='body-good' WHERE article_id='good'")
    event=_pdf_pair(database)
    _check(database,tmp_path,ids={"core:good"},body=body)
    _check(database,tmp_path,kind="meetings",ids={"target-occ"},candidate=event);_approve_all(database)
    manifest=_manifest(database,[("pdf_meeting","target")]);manifest["disposition"]="article_linked_to_dates"
    manifest["relations"]=[{"article_body":_body(manifest["plan"]["participants"][0]["snapshot"]),"event":_scope(manifest["plan"]["participants"][1]),"evidence_ids":["semantic-proof"]}]
    _approve(database,tmp_path,_ingest(database,tmp_path,manifest))
    reader=_reader(database,tmp_path);before=reader.article("good")
    instruction=(_human_activity(database,start="2027-06-10",end="2027-06-12") if manual else source_activity(database,dates=("2027-06-10",)))
    instruction["checked_at"]="2026-10-26T12:00:00Z"
    result=_ingest(database,tmp_path,instruction)
    if not manual:
        assert reader.article("good")==before
        receipt=_date_review(database,tmp_path,result["candidate_sha256"]);receipt["reviewed_at"]="2026-10-26T12:01:00Z"
        approve_date(database,tmp_path,receipt)
    def check(current):
        value=current.article("good")
        assert value["identity_mapping"]["disposition"]=="article_linked_to_dates"
        assert value["identity_mapping"]["body"]=={"content_version_id":"body-good","body_sha256":sha}
        assert value["article_event_relations"]==before["article_event_relations"]
        assert value["is_activity"] is False
        for field in ("title","summary","current_version_id"):assert value[field]==before[field]
        assert value["activity_dates"] and value["activity_dates"][0]["start_date"]=="2027-06-10"
        assert "good" in {row["article_id"] for row in current.articles()["items"]}
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):
            check(reader)
            if direct:
                projection=RegistryListProjection(reader)
                try:assert "good" in {row["article_id"] for row in projection.articles(page=1,page_size=20)["items"]}
                finally:projection.close()
    root=tmp_path/"static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json");check(RegistryReader.from_public_export(root))
    if manual:
        # Unchanged-body inheritance cannot reinterpret the date instruction as classification.
        with sqlite3.connect(database) as db:
            next_sha=stage_snapshot(db,snapshot_entity(db,"article","good"))
            fixture_approve(db,next_sha,{"basis":"unchanged approved body fixture"},status="accepted_legacy")
        check(reader)



def test_display_http_old_article_id_uses_current_visible_canonical_group(tmp_path, monkeypatch):
    import api_server
    from fastapi.testclient import TestClient
    from climate_registry.display import display_snapshot
    from climate_registry.publication import set_visibility
    from climate_registry.read_api import RegistryNotFoundError
    database = _seed(tmp_path)
    _merge_identity(database, tmp_path, [("article", "unchecked")])
    reader = _reader(database, tmp_path)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    client = TestClient(api_server.app)
    def check(visible):
        with display_snapshot(reader) as display:
            assert display is not None
            for identity in ("good", "unchecked"):
                response = client.get("/api/registry/articles/" + identity)
                assert response.status_code == (200 if visible else 404), response.text
                if visible:
                    assert response.json()["article_id"] == "good"
                    assert display.article(identity)["article_id"] == "good"
                else:
                    with pytest.raises(RegistryNotFoundError): display.article(identity)
        items = client.get("/api/registry/articles").json()["items"]
        assert [item["article_id"] for item in items] == (["good"] if visible else [])
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT count(*) FROM display_details WHERE kind='article'").fetchone()[0] == int(visible)
    check(True)
    set_visibility(database, "article", "unchecked", False); check(False)
    set_visibility(database, "article", "good", True); check(True)


@pytest.mark.parametrize("selection", ["entity", "occurrences"])
def test_later_pdf_document_uses_approved_whole_parent_and_fresh_source_review(tmp_path, monkeypatch, selection):
    from climate_monitor.pdf_intake import import_pdf_reports
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.publication import stage_entities
    from climate_registry.read_api import RegistryReader
    from test_pdf_intake import _report_pdf
    from verified_fixtures import seed_verified_articles
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    database = _seed(tmp_path)
    first = tmp_path / "first.pdf"; _report_pdf(first, include_article=True)
    bundle = import_pdf_reports([first])
    source = bundle["articles"][0]["article_id"]
    old_ids = {item["occurrence_id"] for item in bundle["articles"][0]["occurrences"]}
    persist_pdf_intake(database, tmp_path/"backups", bundle)
    with sqlite3.connect(database) as db: seed_verified_articles(db, occurrence_ids=old_ids)
    _approve_all(database)
    manifest = _manifest(database, [("pdf_article", source)])
    scope = _scope(manifest["plan"]["participants"][1])
    if selection == "occurrences":
        scope.update(selection=selection, content_version_id=None, body_sha256=None)
    manifest["aliases"] = [{"source": scope, "same_object": True, "evidence_ids": ["semantic-proof"]}]
    mapped = _ingest(database, tmp_path, manifest); _approve(database, tmp_path, mapped)
    with sqlite3.connect(database) as db:
        old_rows = db.execute("SELECT * FROM pdf_intake_article_occurrences").fetchall()
        old_receipts = db.execute("SELECT * FROM registry_reviews").fetchall()
    second = tmp_path / "second.pdf"; _report_pdf(second, "5–6 Sep 2026", include_article=True)
    incoming = import_pdf_reports([second])
    assert incoming["articles"][0]["article_id"] == source
    new_ids = {item["occurrence_id"] for item in incoming["articles"][0]["occurrences"]} - old_ids
    assert new_ids
    persist_pdf_intake(database, tmp_path/"backups", incoming)
    target = ("article", "good") if selection == "entity" else ("pdf_article", source)
    with sqlite3.connect(database) as db:
        assert canonical_entity(db, "pdf_article", source) == target
        baseline = json.loads(db.execute("SELECT published_baselines_json FROM pdf_source_coverage WHERE document_sha256=?", (incoming["source_coverage"][0]["source_document_sha256"],)).fetchone()[0])
        article_baseline = next(row for row in baseline if row.get("observation_article_id") == source)
        assert (article_baseline["entity_kind"], article_baseline["entity_id"]) == target
        assert article_baseline["requires_downstream_recheck"] is True
        latest, published = db.execute("SELECT latest_candidate_sha256,published_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", target).fetchone()
        assert latest != published
        assert all(row in db.execute("SELECT * FROM pdf_intake_article_occurrences").fetchall() for row in old_rows)
        assert all(row in db.execute("SELECT * FROM registry_reviews").fetchall() for row in old_receipts)
    reader = _reader(database, tmp_path)
    assert not new_ids & {row["occurrence_id"] for row in reader.article("good").get("pdf_occurrences", [])}
    with sqlite3.connect(database) as db: seed_verified_articles(db, occurrence_ids=new_ids)
    _approve_all(database)
    def check(current):
        occurrences = {row["occurrence_id"] for row in current.article("good").get("pdf_occurrences", [])}
        assert old_ids <= occurrences
        if selection == "entity":
            assert new_ids <= occurrences
        else:
            assert not new_ids & occurrences
            assert new_ids <= {row["occurrence_id"] for row in current.pdf_article(source)["occurrences"]}
        report = current.pdf_report(incoming["source_coverage"][0]["source_document_sha256"])
        assert report["article_count"] == 1
    for direct in (True, False):
        with reader.public_snapshot(direct=direct): check(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    check(RegistryReader.from_public_export(root))


@pytest.mark.parametrize("action", ["revoke", "replace"])
def test_exact_review_can_revoke_or_replace_mistaken_alias_without_erasing_history(tmp_path, action):
    from climate_registry.read_api import RegistryReader
    from climate_registry.acquisition import _article_identity
    database = _seed(tmp_path); _identity_pdf_sources(database)
    _merge_identity(database, tmp_path, [("article", "unchecked"), ("pdf_article", "pdf-first")])
    reader = _reader(database, tmp_path)
    current = reader.article("good")["identity_mapping"]
    source_alias = next(alias for alias in current["aliases"] if alias["source"]["entity_id"] == "pdf-first")
    with sqlite3.connect(database) as db:
        original = {table: db.execute("SELECT * FROM " + table).fetchall() for table in ("articles", "article_versions", "pdf_intake_article_occurrences", "registry_reviews")}
    manifest = _manifest(database, [("pdf_article", "pdf-first")])
    manifest["alias_operations"] = [{"action": action, "alias_sha256": digest(source_alias), "evidence_ids": ["semantic-proof"]}]
    if action == "replace":
        scope = _scope(manifest["plan"]["participants"][1]); scope.update(selection="occurrences",content_version_id=None,body_sha256=None)
        manifest["aliases"] = [{"source":scope,"same_object":True,"evidence_ids":["semantic-proof"]}]
    bad = deepcopy(manifest); bad["alias_operations"][0]["alias_sha256"] = "0" * 64
    with pytest.raises(RegistryInputError, match="alias operation digest.*current approved alias scope"):
        _ingest(database, tmp_path, bad)
    staged = _ingest(database, tmp_path, manifest)
    assert reader.pdf_article("pdf-first")["core_article_id"] == "good"
    _approve(database, tmp_path, staged)
    def check(current_reader):
        mapping = current_reader.article("good")["identity_mapping"]
        assert current_reader.article("unchecked")["article_id"] == "good"
        assert mapping["revoked_aliases"][0]["alias_sha256"] == digest(source_alias)
        active = [alias for alias in mapping["aliases"] if alias["source"]["entity_id"] == "pdf-first"]
        assert len(active) == (1 if action == "replace" else 0)
        if active: assert active[0]["source"]["selection"] == "occurrences"
        assert [row["occurrence_id"] for row in current_reader.article("good").get("pdf_occurrences", [])] == (["occ-first"] if action == "replace" else [])
        if action == "revoke": assert current_reader.pdf_article("pdf-first").get("core_article_id") is None
    for direct in (True,False):
        with reader.public_snapshot(direct=direct): check(reader)
    root=tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json"); check(RegistryReader.from_public_export(root))
    with sqlite3.connect(database) as db:
        for table, rows in original.items():
            assert all(row in db.execute("SELECT * FROM " + table).fetchall() for row in rows)
        assert _article_identity(db, {"canonical_url":"https://example.org/unchecked"}) == "good"
    # Empty aliases remain cumulative, even after an explicit revocation.
    _approve(database, tmp_path, _ingest(database, tmp_path, _manifest(database)))
    check(reader)


def test_explicit_article_revoke_allows_new_reviewed_reverse_mapping_without_cycle(tmp_path):
    from climate_registry.identity import approved_aliases
    from climate_registry.acquisition import _article_identity
    database = _seed(tmp_path)
    _merge_identity(database,tmp_path,[("article","unchecked")])
    reader = _reader(database,tmp_path)
    alias = reader.article("good")["identity_mapping"]["aliases"][0]
    manifest = _manifest(database,[("article","unchecked")])
    manifest["alias_operations"]=[{"action":"revoke","alias_sha256":digest(alias),"evidence_ids":["semantic-proof"]}]
    _approve(database,tmp_path,_ingest(database,tmp_path,manifest))
    assert {row["article_id"] for row in reader.articles()["items"]} == {"good","unchecked"}
    with sqlite3.connect(database) as db:
        assert _article_identity(db,{"canonical_url":"https://example.org/unchecked"}) == "unchecked"
    _merge_identity(database,tmp_path,[("article","good")],identity="unchecked")
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):
            assert reader.article("good")["article_id"] == "unchecked"
            assert [row["article_id"] for row in reader.articles()["items"]] == ["unchecked"]
    with sqlite3.connect(database) as db:
        assert approved_aliases(published_snapshots(db,("article",))) == {("article","good"):("article","unchecked")}


@pytest.mark.parametrize("mutation", ["missing_participant", "missing_replacement", "unexpected_readd", "duplicate_operation"])
def test_alias_operations_require_exact_participant_and_explicit_scope(tmp_path, mutation):
    database = _seed(tmp_path)
    _merge_identity(database, tmp_path, [("article", "unchecked")])
    alias = _reader(database, tmp_path).article("good")["identity_mapping"]["aliases"][0]
    manifest = _manifest(database, [] if mutation == "missing_participant" else [("article", "unchecked")])
    operation = {"action":"revoke", "alias_sha256":digest(alias), "evidence_ids":["semantic-proof"]}
    manifest["alias_operations"] = [operation]
    if mutation == "missing_replacement": operation["action"] = "replace"
    if mutation == "unexpected_readd":
        manifest["aliases"] = [{"source":_scope(manifest["plan"]["participants"][1]),"same_object":True,"evidence_ids":["semantic-proof"]}]
    if mutation == "duplicate_operation": manifest["alias_operations"].append(deepcopy(operation))
    with pytest.raises(RegistryInputError, match="alias|scope|participant"):
        _ingest(database, tmp_path, manifest)
    assert _reader(database,tmp_path).article("unchecked")["article_id"] == "good"


def test_revocation_holds_transferred_human_date_instead_of_restoring_it_silently(tmp_path):
    from test_article_content_dates import _manual
    from test_owner_checks import _ingest as ingest_date
    database = _seed(tmp_path)
    ingest_date(database,tmp_path,_manual(database))
    _merge_identity(database,tmp_path,[("article","good")],identity="unchecked")
    reader = _reader(database,tmp_path)
    before = reader.article("unchecked")
    alias = before["identity_mapping"]["aliases"][0]
    manifest = _manifest(database,[("article","good")],identity="unchecked")
    manifest["alias_operations"]=[{"action":"revoke","alias_sha256":digest(alias),"evidence_ids":["semantic-proof"]}]
    with pytest.raises(RegistryInputError, match="held: transferred human date requires an explicit date authorization correction"):
        _ingest(database,tmp_path,manifest)
    after = reader.article("unchecked")
    assert after["manual_date_overrides"] == before["manual_date_overrides"]
    assert reader.article("good")["article_id"] == "unchecked"



def test_explicit_revoke_keeps_new_target_date_and_restores_source_own_authorization(tmp_path):
    from test_article_content_dates import _manual
    from test_owner_checks import _ingest as ingest_date
    database = _seed(tmp_path)
    ingest_date(database,tmp_path,_manual(database))
    _merge_identity(database,tmp_path,[("article","good")],identity="unchecked")
    correction = _manual(database, fields={"publication_date":"2026-10-03"})
    correction["plan"] = plan_owner_check(database,"article","unchecked",date_fields=["publication_date"],human_date_override=True)["plan"]
    correction["observations"][0]["target"] = correction["plan"]["targets"][0]
    correction["manual_authorization"]["instruction"] = correction["manual_authorization"]["instruction"].replace("record good:","record unchecked:")
    ingest_date(database,tmp_path,correction)
    reader = _reader(database,tmp_path)
    authorization = reader.article("unchecked")["manual_date_overrides"]["publication_date"]
    alias = reader.article("unchecked")["identity_mapping"]["aliases"][0]
    manifest = _manifest(database,[("article","good")],identity="unchecked")
    manifest["alias_operations"]=[{"action":"revoke","alias_sha256":digest(alias),"evidence_ids":["semantic-proof"]}]
    _approve(database,tmp_path,_ingest(database,tmp_path,manifest))
    assert reader.article("good")["article_id"] == "good"
    assert reader.article("good")["publication_date"] == "2026-10-02"
    assert reader.article("unchecked")["publication_date"] == "2026-10-03"
    assert reader.article("unchecked")["manual_date_overrides"]["publication_date"] == authorization



def test_whole_article_group_merge_does_not_promote_partial_pdf_locator_to_entity_authority(tmp_path):
    from climate_registry.acquisition import _article_identity
    database = _seed(tmp_path); _identity_pdf_sources(database)
    partial = _manifest(database,[("pdf_article","pdf-first")])
    scope = _scope(partial["plan"]["participants"][1])
    scope.update(selection="occurrences",content_version_id=None,body_sha256=None)
    partial["aliases"]=[{"source":scope,"same_object":True,"evidence_ids":["semantic-proof"]}]
    _approve(database,tmp_path,_ingest(database,tmp_path,partial))
    _merge_identity(database,tmp_path,[("article","good")],identity="unchecked")
    reader=_reader(database,tmp_path)
    mapping=reader.article("unchecked")["identity_mapping"]
    retained=next(alias for alias in mapping["aliases"] if alias["source"]["entity_id"] == "pdf-first")
    assert retained["source"] == scope
    assert reader.article("good")["article_id"] == "unchecked"
    assert [row["occurrence_id"] for row in reader.article("unchecked")["pdf_occurrences"]] == ["occ-first"]
    with sqlite3.connect(database) as db:
        source_url=db.execute("SELECT canonical_url FROM pdf_intake_articles WHERE article_id='pdf-first'").fetchone()[0]
        assert canonical_entity(db,"pdf_article","pdf-first") == ("pdf_article","pdf-first")
        assert _article_identity(db,{"canonical_url":source_url}) != "unchecked"
        assert source_url not in mapping["locators"]
        assert _article_identity(db,{"canonical_url":source_url,"semantic_identity":{"key":"different-material"}}) != "unchecked"



def test_git_install_builds_one_canonical_target_group_for_all_articles(tmp_path, monkeypatch):
    import climate_registry.publication as publication
    database = _seed(tmp_path)
    _merge_identity(database,tmp_path,[("article","unchecked")])
    root=tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json")
    real_aliases=publication._identity_aliases
    real_groups=publication._information_groups
    real_snapshot=publication.snapshot_entity
    calls={"aliases":[],"groups":[],"snapshots":[]}
    def aliases(connection):
        value=real_aliases(connection);calls["aliases"].append(value);return value
    def groups(connection, **kwargs):
        assert kwargs["identity_aliases"] is calls["aliases"][0]
        value=real_groups(connection,**kwargs);calls["groups"].append(value);return value
    def snapshot(connection,kind,identity,**kwargs):
        assert kwargs["identity_aliases"] is calls["aliases"][0]
        assert kwargs["information_groups"] is calls["groups"][0]
        value=real_snapshot(connection,kind,identity,**kwargs)
        assert (value["entity_kind"],value["entity_id"]) == ("article","good")
        assert value["information_targets"] == calls["groups"][0][("article","good")]
        calls["snapshots"].append(identity)
        return value
    monkeypatch.setattr(publication,"_identity_aliases",aliases)
    monkeypatch.setattr(publication,"_information_groups",groups)
    monkeypatch.setattr(publication,"snapshot_entity",snapshot)
    publication.install_git_snapshot(database,root/"wiki")
    assert len(calls["aliases"]) == len(calls["groups"]) == 1
    assert set(calls["snapshots"]) == {"good","unchecked"}
    assert calls["aliases"][0] == {("article","unchecked"):("article","good")}
    assert {target["entity_id"] for target in calls["groups"][0][("article","good")]} == {"good","unchecked"}
    reader=_reader(database,tmp_path)
    assert reader.article("unchecked")["article_id"] == "good"
    assert [row["article_id"] for row in reader.articles()["items"]] == ["good"]


def test_audit_build_reuses_one_exact_information_group_for_all_articles(tmp_path, monkeypatch):
    import climate_registry.publication as publication
    from climate_registry.audit import build_audit_registry
    from test_climate_registry_audit import _weekly,_item
    source=tmp_path/"sources";source.mkdir()
    (source/"climate-monitor-2026-08-03.md").write_text(_weekly("2026-08-03",
        _item("First title","First summary","https://example.org/first"),
        _item("Second title","Second summary","https://example.org/second")),encoding="utf-8")
    real_aliases=publication._identity_aliases
    real_groups=publication._information_groups
    real_snapshot=publication.snapshot_entity
    calls={"aliases":[],"groups":[],"snapshots":[]}
    def aliases(connection):
        value=real_aliases(connection);calls["aliases"].append(value);return value
    def groups(connection,**kwargs):
        assert kwargs["identity_aliases"] is calls["aliases"][0]
        value=real_groups(connection,**kwargs);calls["groups"].append(value);return value
    def snapshot(connection,kind,identity,**kwargs):
        assert kwargs["identity_aliases"] is calls["aliases"][0]
        assert kwargs["information_groups"] is calls["groups"][0]
        value=real_snapshot(connection,kind,identity,**kwargs)
        assert value["information_targets"] == calls["groups"][0][(kind,identity)]
        calls["snapshots"].append(identity);return value
    monkeypatch.setattr(publication,"_identity_aliases",aliases)
    monkeypatch.setattr(publication,"_information_groups",groups)
    monkeypatch.setattr(publication,"snapshot_entity",snapshot)
    database=tmp_path/"audit.sqlite3"
    assert build_audit_registry(source,database,tmp_path/"audit")["unique_articles"] == 2
    assert len(calls["aliases"]) == len(calls["groups"]) == 1
    assert len(set(calls["snapshots"])) == 2
    assert calls["aliases"][0] == {}



def test_pdf_canonical_direction_rejects_losing_approved_core_body_and_old_id(tmp_path):
    import hashlib
    from climate_registry.read_api import RegistryReader
    database=_seed(tmp_path)
    body="Title good. Summary good. Same climate report landing and download."
    sha=hashlib.sha256(body.encode()).hexdigest()
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_content_versions VALUES('body-good','good',?,?,?,'text/html',NULL,'fixture','v1','2026-10-01T00:00:00Z')",(sha,body,sha))
        db.execute("UPDATE articles SET current_content_version_id='body-good',display_policy='full_markdown' WHERE article_id='good'")
    _check(database,tmp_path,ids={"core:good"},body=body);_approve_all(database)
    _identity_pdf_sources(database)
    reader=_reader(database,tmp_path);before=reader.article("good")
    assert before["content"]["markdown"] == body
    with sqlite3.connect(database) as db:
        retained={table:db.execute("SELECT * FROM "+table).fetchall() for table in ("articles","article_content_versions","article_versions","registry_reviews")}
    manifest=plan_owner_check(database,"pdf_article","pdf-first",identity_correction=True,identity_participants=[("article","good")])
    source=_scope(manifest["plan"]["participants"][1])
    manifest.update(actor={"id":"identity-author","task_id":"identity-author-task"},authorization_reference="owner same-material correction",audit_reference="audit/core-body-direction",checked_at="2026-10-25T18:00:00Z",
        evidence=[{"id":"core-proof","source":source,"text":body,"excerpt":"Same climate report","reason":"Retained same-material declaration."}],
        aliases=[{"source":source,"same_object":True,"evidence_ids":["core-proof"]}])
    with pytest.raises(RegistryInputError,match="Article.*canonical|canonical.*Article|body"):
        _ingest(database,tmp_path,manifest)
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):
            assert reader.article("good")["content"]["markdown"] == body
            assert reader.article("good")["article_id"] == "good"
    root=tmp_path/"static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json")
    assert RegistryReader.from_public_export(root).article("good")["content"]["markdown"] == body
    with sqlite3.connect(database) as db:
        for table,rows in retained.items():assert db.execute("SELECT * FROM "+table).fetchall() == rows


def test_reviewed_url_move_reuses_same_retained_material_key_and_keeps_other_keys_distinct(tmp_path,monkeypatch):
    from climate_registry.acquisition import store_acquisition_batch,load_acquisition_batch,_article_identity
    from climate_registry.read_api import RegistryReader
    from test_issue112_acquisition import _item,_batch
    from test_article_content_dates import ordered_source_check_fixture_clock
    # Keep these sequential source checks ordered even when the host clock steps back.
    ordered_source_check_fixture_clock.__wrapped__(monkeypatch)
    database=_seed(tmp_path,acquisition_source=True)
    body="Article. Search excerpt. Same climate report landing and download."
    old_url="https://example.org/keyed-report";new_url="https://example.org/moved-keyed-report"
    def capture(url,batch,key):
        item=_item(url,body=body,discovery_kind="site",discovery_ref="keyed",discovery_search_ref=None)
        item["semantic_identity"]={"key":key,"excerpt":"climate report","reason":"The source explicitly identifies this annual material."}
        store_acquisition_batch(database,_batch([item],batch_id=batch,searches=[],search_decision={"status":"no_search","reason":"site-only"}))
        identity=load_acquisition_batch(database,batch)["items"][0]["article_id"]
        _check(database,tmp_path,ids={"core:"+identity},body=body);_approve_all(database)
        return identity
    first=capture(old_url,"key-first","report-2026")
    with sqlite3.connect(database) as db:
        previous=db.execute("SELECT * FROM acquisition_items WHERE article_id=?",(first,)).fetchall()
        body_ids={row[0] for row in db.execute("SELECT content_version_id FROM article_content_versions WHERE article_id=?",(first,))}
    manifest=_manifest(database,identity=first);manifest["canonical_url"]=new_url
    _approve(database,tmp_path,_ingest(database,tmp_path,manifest))
    second=capture(new_url,"key-second","report-2026")
    assert second == first
    distinct=capture(new_url,"key-next-edition","report-2027")
    assert distinct != first
    with sqlite3.connect(database) as db:
        assert _article_identity(db,{"canonical_url":new_url,"semantic_identity":{"key":"report-2026"}}) == first
        assert _article_identity(db,{"canonical_url":new_url,"semantic_identity":{"key":"report-2027"}}) == distinct
        rows=db.execute("SELECT * FROM acquisition_items WHERE article_id=?",(first,)).fetchall()
        assert all(row in rows for row in previous)
        assert db.execute("SELECT count(DISTINCT article_id) FROM article_content_versions WHERE markdown_content=? AND article_id<>?",(body,distinct)).fetchone()[0] == 1
        assert body_ids <= {row[0] for row in db.execute("SELECT content_version_id FROM article_content_versions WHERE article_id=?",(first,))}
        origins=[json.loads(row[0]) for row in db.execute("SELECT origins_json FROM acquisition_items WHERE article_id=?",(first,))]
        assert {origin["semantic_identity"]["key"] for group in origins for origin in group} == {"report-2026"}
    reader=_reader(database,tmp_path)
    def check(current):
        keyed=[row for row in current.articles()["items"] if row["article_id"] in {first,distinct}]
        assert len(keyed) == 2 and {row["article_id"] for row in keyed} == {first,distinct}
        assert current.article(first)["canonical_url"] == new_url
        assert {row["raw_url"] for row in current.article(first)["acquisition_observations"]} == {old_url,new_url}
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):check(reader)
    root=tmp_path/"static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json");check(RegistryReader.from_public_export(root))


def test_unique_url_and_article_label_do_not_merge_distinct_pdf_edition_without_review(tmp_path):
    from test_registry_publication import _pdf
    from climate_registry.publication import snapshot_entity
    from climate_registry.schema import reconcile_pdf_article_links
    from climate_registry.read_api import RegistryReader
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE article_versions SET observed_title='Annual climate report 2027',observed_summary='2027 edition findings' WHERE article_id='good'")
    _check(database,tmp_path,ids={"core:good"},body="Annual climate report 2027. 2027 edition findings.");_approve_all(database)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-2026",url="https://example.org/good",occurrence_id="old-report",occurrence={"title":"Annual climate report 2026","summary":"2026 edition findings","summary_basis":"verbatim_pdf_article_row"})
        assert db.execute("SELECT core_article_id FROM pdf_intake_articles WHERE article_id='pdf-2026'").fetchone()[0] is None
        assert canonical_entity(db,"pdf_article","pdf-2026") == ("pdf_article","pdf-2026")
        source=snapshot_entity(db,"pdf_article","pdf-2026")
        assert (source["entity_kind"],source["entity_id"]) == ("pdf_article","pdf-2026")
        assert not snapshot_entity(db,"article","good")["tables"]["pdf_intake_article_occurrences"]
        db.execute("UPDATE pdf_intake_articles SET type_safe_classification_json=? WHERE article_id='pdf-2026'",(json.dumps({"label":"article"}),))
        reconcile_pdf_article_links(db)
        assert db.execute("SELECT core_article_id,confirmation_basis FROM pdf_intake_articles WHERE article_id='pdf-2026'").fetchone() == (None,None)
        from verified_fixtures import seed_verified_articles
        seed_verified_articles(db,occurrence_ids={"old-report"})
    _approve_all(database)
    reader=_reader(database,tmp_path)
    def check(current):
        article=current.article("good")
        assert article["title"] == "Annual climate report 2027" and not article.get("pdf_occurrences")
        pdf=current.pdf_article("pdf-2026")
        assert pdf.get("core_article_id") is None
        assert [row["occurrence_id"] for row in pdf["occurrences"]] == ["old-report"]
        assert [row["article_id"] for row in current.pdf_articles_all()] == ["pdf-2026"]
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):check(reader)
    root=tmp_path/"static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json");check(RegistryReader.from_public_export(root))
    with sqlite3.connect(database) as db:
        assert not db.execute("SELECT 1 FROM registry_reviews WHERE json_extract(receipt_json,'$.schema_version')='owner-agent-identity-review.v1'").fetchone()
        assert db.execute("SELECT published_candidate_sha256 IS NOT NULL FROM registry_publication WHERE entity_kind='pdf_article' AND entity_id='pdf-2026'").fetchone()[0]



def test_unlinked_approved_pdf_reference_keeps_own_report_scope_without_core_facts(tmp_path):
    from test_registry_publication import _pdf
    from climate_registry.read_api import RegistryReader
    database=_seed(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-reference",url="https://example.org/good",verified=False,
            occurrence={"title":"PRIVATE unrelated title","summary":"PRIVATE unreviewed PDF facts","summary_basis":"verbatim_pdf_article_row"})
        originals=db.execute("SELECT * FROM pdf_intake_article_occurrences").fetchall()
    _approve_all(database)
    assert _reader(database,tmp_path).pdf_articles_all()==[]
    from test_owner_checks import _manifest as source_manifest
    manifest=source_manifest(database,kind="pdf_article",identity="pdf-reference",occurrence="occurrence")
    staged=_ingest(database,tmp_path,manifest)
    _approve(database,tmp_path,staged)
    reader=_reader(database,tmp_path)
    def check(current):
        core=current.article("good")
        assert not core.get("pdf_occurrences")
        item=current.pdf_article("pdf-reference")
        assert item.get("core_article_id") is None
        assert not item.get("metadata_only") and item["title"] == "Climate risk findings"
        assert not item.get("verified_primary_reference")
        assert item["summary"] == "Flood exposure increased in the study region."
        assert item["owner_agent_verification"]["review_id"]
        assert item["published_candidate_sha256"] == staged["candidate_sha256"]
        assert [row["occurrence_id"] for row in item["occurrences"]] == ["occurrence"]
        report=current.pdf_report("f"*64)
        assert report["article_count"] == 1
        assert report["articles"][0]["article_id"] == "pdf-reference"
        assert "PRIVATE" not in json.dumps(item)+json.dumps(report)
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):check(reader)
    root=tmp_path/"static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json");check(RegistryReader.from_public_export(root))
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM pdf_intake_article_occurrences").fetchall() == originals



def test_identity_then_owner_review_keeps_history_binding_and_rejects_changed_live_source(tmp_path):
    from test_owner_checks import _manifest as owner_manifest
    from climate_registry.owner_checks import approve_owner_check
    from test_registry_publication import _pdf
    from test_owner_checks import _approve_exact_pdf_alias
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-first",url="https://example.org/download",occurrence_id="occ-first",verified=False)
    _approve_exact_pdf_alias(database,tmp_path,identity="pdf-first",occurrence="occ-first")
    published=_reader(database,tmp_path).article("good")["published_candidate_sha256"]
    manifest=owner_manifest(database,kind="pdf_article",identity="pdf-first",occurrence="occ-first")
    staged=_ingest(database,tmp_path,manifest)
    receipt=_review(database,tmp_path,staged["candidate_sha256"])
    with sqlite3.connect(database) as db:
        candidate=json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(staged["candidate_sha256"],)).fetchone()[0])
        assert digest(candidate["approved_identity_history"][0]["snapshot"]) == published
        db.execute("UPDATE articles SET last_seen='2026-10-27' WHERE article_id='good'")
    with pytest.raises(RegistryInputError,match="live source|held|invalid"):
        approve_owner_check(database,receipt,lifecycle_root=tmp_path/"runs",queue_dir=tmp_path/"queue",backup_dir=tmp_path/"backups",apply=True)
    assert _reader(database,tmp_path).article("good")["published_candidate_sha256"] == published



def test_exact_whole_alias_owner_fields_keep_native_body_and_cross_consumer_provenance(tmp_path,monkeypatch):
    from test_owner_checks import _owner_article_with_private_metadata
    from climate_registry.publication import public_wiki_pages
    from climate_registry.read_api import RegistryReader
    from fastapi.testclient import TestClient
    from agentic_wiki.chat_evidence import article_text,source_urls
    import api_server
    final="https://example.org/official/current-report"
    database,_=_owner_article_with_private_metadata(tmp_path,"linked",final,report_appearance=True,native_body=True)
    reader=_reader(database,tmp_path)
    with sqlite3.connect(database) as db:
        body=db.execute("SELECT markdown_content FROM article_content_versions WHERE content_version_id='native-owner-body'").fetchone()[0]
        originals={table:db.execute("SELECT * FROM "+table).fetchall() for table in ("articles","article_versions","article_content_versions","pdf_intake_article_occurrences")}
    def check(current):
        item=current.article("good")
        assert item["content"]["content_version_id"] == "native-owner-body" and item["content"]["markdown"] == body
        assert item["checked_source_url"] == final and source_urls(item) == [final]
        assert item["summary"] == "Flood exposure increased in the study region."
        assert item["summary_provenance"] == "owner_agent_verified_source"
        owner=item["owner_agent_verification"]
        with sqlite3.connect(database) as db:
            candidate,receipt=db.execute("SELECT candidate_sha256,receipt_json FROM registry_reviews WHERE review_id=?",(owner["review_id"],)).fetchone()
        assert candidate == item["published_candidate_sha256"]
        assert owner["capture_sha256"] in {binding["capture_sha256"] for binding in json.loads(receipt)["source_checks"]}
        assert not item["categories"] and not item["keywords"]
        assert "PRIVATE" not in json.dumps(item)+article_text(item,{})
        assert current.pdf_article("pdf-good")["checked_source_url"] == final
        report=current.report("2026-09-28")
        assert report["articles"][0]["checked_source_url"] == final
        assert report["articles"][0]["summary"] == item["summary"]
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):check(reader)
    pages=public_wiki_pages(database,reader=reader)
    assert final in pages["article-good.md"] and body in pages["article-good.md"]
    assert final in pages["climate-monitor-2026-09-28.md"] and "PRIVATE" not in json.dumps(pages)
    root=tmp_path/"static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json");check(RegistryReader.from_public_export(root))
    monkeypatch.setattr(api_server,"_registry_reader",lambda:reader)
    client=TestClient(api_server.app)
    response=client.get("/api/registry/articles/good");assert response.status_code == 200,response.text
    assert response.json()["content"]["markdown"] == body and response.json()["checked_source_url"] == final
    cards=client.get("/api/registry/articles?include_pdf=true").json()["items"]
    assert next(row for row in cards if row["article_id"] == "good")["checked_source_url"] == final
    with sqlite3.connect(database) as db:
        for table,rows in originals.items():assert db.execute("SELECT * FROM "+table).fetchall() == rows


@pytest.mark.parametrize("selection",[None,"occurrences"])
def test_pdf_owner_fields_do_not_promote_unlinked_or_partial_source_to_article_authority(tmp_path,selection):
    from test_registry_publication import _pdf
    from test_owner_checks import _manifest as owner_manifest
    database=_seed(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-owner",url="https://example.org/good",verified=False)
    manifest=owner_manifest(database,kind="pdf_article",identity="pdf-owner",occurrence="occurrence",final_url="https://example.org/pdf-checked")
    staged=_ingest(database,tmp_path,manifest);_approve(database,tmp_path,staged)
    if selection:
        correction=_manifest(database,[("pdf_article","pdf-owner")])
        source=_scope(correction["plan"]["participants"][1]);source.update(selection="occurrences",content_version_id=None,body_sha256=None)
        correction["aliases"]=[{"source":source,"same_object":True,"evidence_ids":["semantic-proof"]}]
        _approve(database,tmp_path,_ingest(database,tmp_path,correction))
    reader=_reader(database,tmp_path)
    def check(current):
        item=current.article("good")
        assert item["title"] == "Title good" and item["summary_provenance"] != "owner_agent_verified_source"
        assert not item.get("owner_agent_verification") and not item.get("checked_source_url")
        assert current.pdf_article("pdf-owner")["checked_source_url"] == "https://example.org/pdf-checked"
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):check(reader)
    from climate_registry.read_api import RegistryReader
    root=tmp_path/"partial-static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json");check(RegistryReader.from_public_export(root))


def test_public_pdf_merge_preserves_exact_occurrences_and_safe_check_shape():
    from climate_registry.read_api import _public_source_checks
    from climate_registry.publication import _merge_pdf_article_payloads
    first={"occurrence_id":"first","source_document_sha256":"f"*64,"page":1,
        "raw_url":"https://example.org/shared","summary":"Same retained PDF passage",
        "checks":[{"verification_status":"verified","access_status":"accessible","checked_at":"2026-10-01",
            "source_url":"https://example.org/shared","source_binding":{"private":"internal evidence"}}]}
    _public_source_checks(first)
    assert "website_candidate" not in first["checks"][0] and "source_binding" not in first["checks"][0]
    second={**first,"occurrence_id":"second"}
    merged=_merge_pdf_article_payloads({"occurrences":[first,second]}, {"occurrences":[dict(first)]})
    assert {item["occurrence_id"] for item in merged["occurrences"]} == {"first","second"}
    assert len(merged["occurrences"]) == 2
    checked=next(item for item in merged["occurrences"] if item["occurrence_id"] == "first")
    assert checked["verification_status"] == "verified" and checked["collected_candidate"] is None
    assert "private" not in json.dumps(merged)


@pytest.mark.parametrize("identity",[None, "", "   "])
def test_public_pdf_merge_rejects_missing_exact_occurrence_identity(identity):
    from climate_registry.publication import _merge_pdf_article_payloads
    with pytest.raises(ValueError,match="exact occurrence identity"):
        _merge_pdf_article_payloads({"occurrences":[{"occurrence_id":identity},{"occurrence_id":identity}]},None)


@pytest.mark.parametrize("status,exact",[("accepted_legacy",True),("pass",True),("accepted_legacy",False)])
@pytest.mark.parametrize("running",[False,True])
def test_unverified_standalone_pdf_cannot_gain_source_authority_from_receipt_only(tmp_path,status,exact,running):
    from test_registry_publication import _pdf
    from climate_registry.publication import stage_entities
    from climate_registry.verification import evaluate_snapshot,permitted_view
    database=_seed(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="legacy-reference",url="https://example.org/good",verified=False)
        stage_entities(db)
        sha,snapshot=db.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates WHERE entity_kind='pdf_article'").fetchone()
    snapshot=json.loads(snapshot);receipt={"basis":"retained source association"}
    if running:
        snapshot.setdefault("dependencies",{})["article_check_runs"]=[{"status":"pending"}]
        sha=digest(snapshot)
    review={"candidate_sha256":sha,"status":status,"receipt_json":json.dumps(receipt),
        "review_id":digest([sha,status,receipt]) if exact else "0"*64}
    verdict=evaluate_snapshot(snapshot,reviews=[review])
    assert verdict["eligible"] is False
    view=permitted_view(snapshot,verdict)
    assert view is None


def test_legacy_pdf_reference_does_not_bypass_failed_t1(tmp_path):
    from test_registry_publication import _pdf
    database=_seed(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="failed-reference",url="https://example.org/good",verified=False)
    _check(database,tmp_path,ids={"occurrence"},failed=True)
    _approve_all(database)
    reader=_reader(database,tmp_path)
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):assert reader.pdf_articles_all()==[]
    from climate_registry.read_api import RegistryReader
    root=tmp_path/"failed-legacy-static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json")
    assert RegistryReader.from_public_export(root).pdf_articles_all()==[]


@pytest.mark.parametrize("selection",[None,"entity","occurrences"])
def test_range_pdf_source_reference_requires_approved_exact_mapping(tmp_path,selection):
    from test_registry_publication import _pdf
    from climate_registry.range_reports import _range_source
    from climate_registry.read_api import RegistryReader
    database=_seed(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-range",url="https://example.org/good",occurrence_id="range-occ",verified=False,
            period=("2026-09-14","2026-09-14"))
        row=list(db.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='range-occ'").fetchone())
        row[0]="range-independent";row[3]=3
        raw=json.loads(row[-1]);raw["summary"]="Separate original PDF observation";row[-1]=json.dumps(raw)
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",row)
        db.executemany("INSERT INTO pdf_intake_document_sources VALUES(?,?,?,?)",[
            ("f"*64,"/evidence/source.pdf","source.pdf","2026-08-01T00:00:00Z"),
            ("f"*64,"/evidence/z-copy.pdf","renamed.pdf","2026-09-01T00:00:00Z")])
    _check(database,tmp_path,ids={"core:good","range-occ","range-independent"},
        body="Title good. Summary good. Original PDF title. Original PDF observation. Separate original PDF observation. Same climate report landing and download.")
    _approve_all(database)
    if selection:
        manifest=_manifest(database,[("pdf_article","pdf-range")])
        source=_scope(manifest["plan"]["participants"][1])
        if selection == "occurrences":source.update(selection=selection,occurrence_ids=["range-occ"],content_version_id=None,body_sha256=None)
        manifest["aliases"]=[{"source":source,"same_object":True,"evidence_ids":["semantic-proof"]}]
        _approve(database,tmp_path,_ingest(database,tmp_path,manifest))
    with sqlite3.connect(database) as db:
        originals={table:db.execute("SELECT * FROM "+table).fetchall() for table in (
            "pdf_intake_documents","pdf_intake_document_sources","pdf_intake_article_occurrences","registry_reviews")}
    reader=_reader(database,tmp_path)
    def check(current):
        expected=({"range-occ":"good","range-independent":"good"} if selection == "entity"
            else {"range-occ":"good"} if selection == "occurrences" else {})
        detail=current.pdf_article("pdf-range")
        assert detail.get("canonical_occurrence_mappings",{}) == expected
        collected=next(item for item in current.pdf_articles_all(include_linked=True) if item["article_id"] == "pdf-range")
        assert collected.get("canonical_occurrence_mappings",{}) == expected
        result=_range_source(current,"2026-09-14","2026-09-14")
        updates={item["observation_id"]:item for item in result["pdf_source_updates"]}
        assert set(updates) == {"range-occ","range-independent"}
        for occurrence_id,update in updates.items():
            assert update["source_observations"] == [{"filename":"source.pdf","observed_at":"2026-08-01T00:00:00Z"},
                {"filename":"renamed.pdf","observed_at":"2026-09-01T00:00:00Z"}]
            assert "/evidence/source.pdf" not in json.dumps(update)
            assert update["filename"] == "source.pdf" and update["url"] == "https://example.org/good"
            citation,=update["citations"]
            assert citation["document_sha256"] == "f"*64 and citation["page"] == (2 if occurrence_id == "range-occ" else 3)
            reference=update["record_reference"]
            mapped=selection == "entity" or selection == "occurrences" and occurrence_id == "range-occ"
            if mapped:
                assert reference["entity_kind"] == "article" and reference["entity_id"] == "good"
                assert reference["candidate_sha256"] == current.article("good")["published_candidate_sha256"]
            else:
                assert reference["entity_kind"] == "pdf_article" and reference["entity_id"] == "pdf-range"
                assert update["core_article_id"] is None
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):check(reader)
    root=tmp_path/"range-static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json");check(RegistryReader.from_public_export(root))
    with sqlite3.connect(database) as db:
        for table,rows in originals.items():assert db.execute("SELECT * FROM "+table).fetchall() == rows


@pytest.mark.parametrize("selection",["entity","occurrences",None])
def test_new_pdf_observation_of_explicit_old_core_link_follows_only_whole_article_group(tmp_path,monkeypatch,selection):
    from climate_monitor.pdf_intake import import_pdf_reports
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.publication import _approve as approve_snapshot,information_ready
    from climate_registry.read_api import RegistryReader
    from climate_registry.article_dates import import_observations
    from test_article_dates import _observation,_payload
    from test_pdf_intake import _report_pdf
    monkeypatch.delenv("TYPESAFE_API_KEY",raising=False)
    database=_seed(tmp_path)
    first=tmp_path/"first-linked-source.pdf";_report_pdf(first,include_article=True)
    bundle=import_pdf_reports([first]);parent=bundle["articles"][0]["article_id"]
    old_ids={row["occurrence_id"] for row in bundle["articles"][0]["occurrences"]}
    persist_pdf_intake(database,tmp_path/"backups",bundle)
    # Retained pre-G4 explicit core link, frozen with real source checks before the new merge.
    with sqlite3.connect(database) as db:
        db.execute("UPDATE pdf_intake_articles SET core_article_id='unchecked',confirmation_basis='exact_url_eligible_detail' WHERE article_id=?",(parent,))
    body="Title good. Summary good. Title unchecked. Summary unchecked. Same climate report landing and download. Climate publication. Independent publication summary. Published 2 September 2026. Updated 15 October 2026."
    _check(database,tmp_path,ids={"core:good","core:unchecked",*old_ids},body=body)
    _approve_all(database)
    if selection:
        manifest=_manifest(database,[("article","unchecked")])
        source=_scope(manifest["plan"]["participants"][1])
        if selection == "occurrences":source.update(selection=selection,content_version_id=None,body_sha256=None)
        manifest["aliases"]=[{"source":source,"same_object":True,"evidence_ids":["semantic-proof"]}]
        _approve(database,tmp_path,_ingest(database,tmp_path,manifest))
    owner="good" if selection == "entity" else "unchecked"
    reader=_reader(database,tmp_path)
    before=reader.article(owner)
    with sqlite3.connect(database) as db:
        original={table:db.execute("SELECT * FROM "+table).fetchall() for table in (
            "articles","article_versions","pdf_intake_article_occurrences","registry_reviews")}
    second=tmp_path/"new-linked-source.pdf";_report_pdf(second,"5–6 Sep 2026",include_article=True)
    incoming=import_pdf_reports([second]);assert incoming["articles"][0]["article_id"] == parent
    new_ids={row["occurrence_id"] for row in incoming["articles"][0]["occurrences"]}-old_ids
    assert new_ids
    persist_pdf_intake(database,tmp_path/"backups",incoming)
    date=_observation("page_information","2026-10-15",record_id="new-linked-source-date")
    date.update(article_id=parent,canonical_url=incoming["articles"][0]["canonical_url"])
    date["evidence"].update(date_kind="updated",date_precision="day",source_url=date["canonical_url"],
        field="Updated",text="Updated 15 October 2026")
    import_observations(database,_payload(date),entity_kind="pdf_article",write=True)
    assert reader.article(owner)["published_candidate_sha256"] == before["published_candidate_sha256"]
    assert not new_ids & {row["occurrence_id"] for row in reader.article(owner).get("pdf_occurrences",[])}
    with sqlite3.connect(database) as db:
        assert canonical_entity(db,"pdf_article",parent) == ("article",owner)
        pending=json.loads(db.execute("SELECT c.snapshot_json FROM registry_publication p JOIN registry_candidates c ON c.candidate_sha256=p.latest_candidate_sha256 WHERE p.entity_kind='article' AND p.entity_id=?",(owner,)).fetchone()[0])
        assert not information_ready(db,pending)
    _check(database,tmp_path,ids={"core:good","core:unchecked",*old_ids,*new_ids},body=body)
    with sqlite3.connect(database) as db:
        sha,encoded,published=db.execute("SELECT c.candidate_sha256,c.snapshot_json,p.published_candidate_sha256 FROM registry_publication p JOIN registry_candidates c ON c.candidate_sha256=p.latest_candidate_sha256 WHERE p.entity_kind='article' AND p.entity_id=?",(owner,)).fetchone()
        candidate=json.loads(encoded)
        assert sha != published and information_ready(db,candidate)
        assert new_ids <= {row["occurrence_id"] for row in candidate["information_targets"]}
        assert new_ids <= {row["occurrence_id"] for row in candidate["tables"]["pdf_intake_article_occurrences"]}
        assert any(row["article_id"] == parent and row["observed_at"] == "2026-10-15" for row in candidate["tables"]["pdf_article_date_observations"])
        document=incoming["documents"][0]["source"]["sha256"]
        assert document in {row["document_sha256"] for row in candidate["dependencies"]["pdf_intake_documents"]}
        assert new_ids <= {row["occurrence_id"] for row in candidate["tables"]["article_check_attempts"]}
        receipt={"candidate_sha256":sha,"reviewer":"independent-current-source-reviewer","basis":"Checked exact current source packets and candidate after the identity merge"}
        review=approve_snapshot(db,sha,receipt,status="pass")
        assert db.execute("SELECT candidate_sha256,status FROM registry_reviews WHERE review_id=?",(review,)).fetchone() == (sha,"pass")
    def check(current):
        detail=current.article(owner)
        assert detail["published_candidate_sha256"] == sha
        assert detail.get("updated_date") == before.get("updated_date")
        assert new_ids <= {row["occurrence_id"] for row in detail["pdf_occurrences"]}
        if selection != "entity":
            assert not new_ids & {row["occurrence_id"] for row in current.article("good").get("pdf_occurrences",[])}
        assert current.article("good")["article_id"] == "good"
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):check(reader)
    root=tmp_path/"new-source-static";(root/"wiki").mkdir(parents=True)
    export_public_snapshot(database,root/"wiki/public-registry.json");check(RegistryReader.from_public_export(root))
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT core_article_id,confirmation_basis FROM pdf_intake_articles WHERE article_id=?",(parent,)).fetchone() == ("unchecked","exact_url_eligible_detail")
        for table,rows in original.items():
            current=db.execute("SELECT * FROM "+table).fetchall()
            assert all(row in current for row in rows)



def _http_same_url_pdf_reader(tmp_path, monkeypatch, mode, *, selection=None,same_parent_observation=False):
    from climate_registry.read_api import RegistryReader
    from climate_registry.publication import install_git_snapshot
    from climate_registry.schema import apply_migrations
    from test_registry_publication import _pdf
    from verified_fixtures import seed_verified_articles
    database=_seed(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-edition-2026",url="https://example.org/good",occurrence_id="occ-2026")
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES('pdf-edition-2027','https://example.org/good','2027 independent report','2026-09-01')")
        first=list(db.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='occ-2026'").fetchone())
        second=list(first);second[0],second[1],second[3]="occ-2027","pdf-edition-2027",3
        raw=json.loads(second[-1]);raw.update(title="2027 independent report",summary="2027 independent material",occurrence_id=second[0],page=3)
        second[-1]=json.dumps(raw)
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",second)
        ids={"occ-2026","occ-2027"}
        if same_parent_observation:
            repeated=list(first);repeated[0],repeated[3]="occ-2026-repeat",5
            raw=json.loads(repeated[-1]);raw.update(occurrence_id=repeated[0],page=5)
            repeated[-1]=json.dumps(raw)
            db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",repeated)
            ids.add("occ-2026-repeat")
        if selection == "occurrences":
            remainder=list(first);remainder[0],remainder[3]="occ-remainder",4
            raw=json.loads(remainder[-1]);raw.update(title="Unmapped independent material",summary="Separate remainder",occurrence_id=remainder[0],page=4)
            remainder[-1]=json.dumps(raw)
            db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",remainder)
            ids.add("occ-remainder")
        seed_verified_articles(db,occurrence_ids=ids)
    _approve_all(database)
    if selection:
        manifest=_manifest(database,[("pdf_article","pdf-edition-2026")])
        scope=_scope(manifest["plan"]["participants"][1])
        if selection == "occurrences":
            scope.update(selection=selection,occurrence_ids=["occ-2026"],content_version_id=None,body_sha256=None)
        manifest["aliases"]=[{"source":scope,"same_object":True,"evidence_ids":["semantic-proof"]}]
        approved=_ingest(database,tmp_path,manifest);_approve(database,tmp_path,approved)
        with sqlite3.connect(database) as db:
            view=published_snapshots(db,("article",))[("article","good")]
            assert view["identity_mapping"]["aliases"][0]["source"] == scope
    reader=_reader(database,tmp_path)
    if mode != "dynamic":
        root=tmp_path/"http-export";(root/"wiki").mkdir(parents=True)
        export_public_snapshot(database,root/"wiki/public-registry.json")
        if mode == "static":reader=RegistryReader.from_public_export(root)
        else:
            installed=tmp_path/"http-installed.sqlite3"
            with sqlite3.connect(installed) as db:apply_migrations(db)
            db.close();install_git_snapshot(installed,root/"wiki")
            monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
            reader=RegistryReader(installed,repository_root=root)
    return reader,database,ids


@pytest.mark.parametrize("mode",["static","dynamic","installed"])
@pytest.mark.parametrize("runtime_duplicate",[False,True])
def test_http_lists_preserve_independent_approved_same_url_pdf_identities(tmp_path,monkeypatch,mode,runtime_duplicate):
    from fastapi.testclient import TestClient
    import api_server
    reader,database,ids=_http_same_url_pdf_reader(tmp_path,monkeypatch,mode)
    with sqlite3.connect(database) as db:
        original=db.execute("SELECT * FROM pdf_intake_article_occurrences ORDER BY occurrence_id").fetchall()
    monkeypatch.setattr(api_server,"_registry_reader",lambda:reader)
    monkeypatch.setattr(api_server,"_pdf_registry_view",lambda:(reader,ids if runtime_duplicate else None,None))
    monkeypatch.setattr(api_server,"_range_report_overlay",lambda:(None,None,None))
    client=TestClient(api_server.app)
    expected={"pdf-edition-2026","pdf-edition-2027"}
    for route,all_ids in (("/api/registry/pdf-intake/articles",expected),("/api/registry/articles?include_pdf=true",expected|{"good","unchecked"})):
        response=client.get(route);assert response.status_code == 200,response.text
        payload=response.json()
        assert payload["pagination"]["total"] == len(all_ids)
        assert {row["article_id"] for row in payload["items"]} == all_ids
        pdf=[row for row in payload["items"] if row["article_id"] in expected]
        assert all(row["occurrence_count"] == 1 for row in pdf)
        assert all(not row.get("core_article_id") and not row.get("canonical_display_article_id") for row in pdf)
    for identity,occurrence in (("pdf-edition-2026","occ-2026"),("pdf-edition-2027","occ-2027")):
        detail=client.get("/api/registry/pdf-intake/articles/"+identity)
        assert detail.status_code == 200,detail.text
        assert [row["occurrence_id"] for row in detail.json()["occurrences"]] == [occurrence]
    response=client.get("/api/registry/articles?include_pdf=true&query=2027")
    assert response.status_code == 200 and [row["article_id"] for row in response.json()["items"]] == ["pdf-edition-2027"]
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM pdf_intake_article_occurrences ORDER BY occurrence_id").fetchall() == original


@pytest.mark.parametrize("mode",["static","dynamic","installed"])
@pytest.mark.parametrize("selection",["entity","occurrences"])
@pytest.mark.parametrize("runtime_duplicate",[False,True])
def test_http_lists_merge_only_exact_approved_pdf_ownership(tmp_path,monkeypatch,mode,selection,runtime_duplicate):
    from fastapi.testclient import TestClient
    import api_server
    reader,database,ids=_http_same_url_pdf_reader(tmp_path,monkeypatch,mode,selection=selection)
    monkeypatch.setattr(api_server,"_registry_reader",lambda:reader)
    monkeypatch.setattr(api_server,"_pdf_registry_view",lambda:(reader,ids if runtime_duplicate else None,None))
    monkeypatch.setattr(api_server,"_range_report_overlay",lambda:(None,None,None))
    client=TestClient(api_server.app)
    standalone={"pdf-edition-2027"}|({"pdf-edition-2026"} if selection == "occurrences" else set())
    response=client.get("/api/registry/articles?include_pdf=true");assert response.status_code == 200,response.text
    payload=response.json()
    assert payload["pagination"]["total"] == len(standalone)+2
    assert {row["article_id"] for row in payload["items"]} == standalone|{"good","unchecked"}
    assert next(row for row in payload["items"] if row["article_id"] == "good")["pdf_occurrence_count"] == 1
    remainder=[row for row in payload["items"] if row["article_id"] == "pdf-edition-2026"]
    if selection == "occurrences":
        assert remainder[0]["occurrence_count"] == 1
        detail=client.get("/api/registry/pdf-intake/articles/pdf-edition-2026")
        assert detail.status_code == 200,detail.text
        assert {row["occurrence_id"] for row in detail.json()["occurrences"]} == {"occ-2026","occ-remainder"}
        assert detail.json()["canonical_occurrence_mappings"] == {"occ-2026":"good"}
    response=client.get("/api/registry/pdf-intake/articles");assert response.status_code == 200,response.text
    assert response.json()["pagination"]["total"] == len(standalone)
    assert {row["article_id"] for row in response.json()["items"]} == standalone
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT core_article_id FROM pdf_intake_articles WHERE article_id='pdf-edition-2026'").fetchone()[0] is None
        assert {row[0] for row in db.execute("SELECT occurrence_id FROM pdf_intake_article_occurrences")} == ids



def test_native_dates_alias_retains_reviewed_lineage_across_update_and_revocation(tmp_path,monkeypatch):
    import hashlib
    from climate_registry.acquisition import _stable_id,store_acquisition_batch
    from climate_registry.publication import stage_entities,snapshot_entity,stage_snapshot
    from climate_registry.article_dates import _collection_timestamp
    from climate_registry.item_lifecycle import _article_dependency,_load
    from test_enrichment_contract import _native_contract_review
    from climate_registry.read_api import RegistryReader
    from climate_monitor.meetings import process_batch
    from test_issue136_meetings import _database as native_database,_candidate,_stored_item,_stored_batch
    from verified_fixtures import seed_verified_articles,seed_verified_native_meetings
    from climate_registry.owner_checks import prepare_owner_checks
    body=("Example hosts World Climate Summit 2027 on June 10–12, 2027 in New York. "
        "Registration deadline May 1, 2027. Register https://example.com/register or "
        "https://example.com/registration. Climate risk agenda. PRIVATE local archive marker.")
    # The established native fixture retains the literal body, successful fetch,
    # acquisition row and exact current body; no latest/body ID is patched here.
    database=native_database(tmp_path,[body,body],source_id=_stable_id("source","example.com"))
    urls=["https://example.com/1","https://example.com/2"]
    registrations=["https://example.com/register","https://example.com/registration"]
    def extract(request):
        return {"events":[_candidate(online_url=registrations[urls.index(request["source_url"])])]}
    assert process_batch(database,"batch",prompt_text="retained native event inputs",prompt_version="v1",provider="fixture",model="fixture",extractor=extract)["status"] == "succeeded"
    with sqlite3.connect(database) as db:
        seed_verified_articles(db)
        seed_verified_native_meetings(db)
    _approve_all(database)
    with sqlite3.connect(database) as db:
        target=db.execute("SELECT event_id FROM climate_events WHERE online_url=?",(registrations[0],)).fetchone()[0]
        source=db.execute("SELECT event_id FROM climate_events WHERE online_url=?",(registrations[1],)).fetchone()[0]
        originals={table:db.execute("SELECT * FROM "+table).fetchall() for table in ("climate_events","climate_event_sources","climate_event_versions","article_content_versions","meeting_runs","meeting_run_items","acquisition_batches","registry_reviews")}
        source_rows=[dict(zip([c[1] for c in db.execute("PRAGMA table_info(climate_event_sources)")],row)) for row in originals["climate_event_sources"]]
        original_versions={(row[0],row[1],row[3]) for row in originals["climate_event_versions"]}
        original_article_sha=db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='article-1'").fetchone()[0]
    def manifest():
        value=plan_owner_check(database,"meeting",target,identity_correction=True,identity_participants=[("meeting",source)])
        value.update(actor={"id":"identity-author","task_id":"identity-author-task"},authorization_reference="Owner reconciles this same event edition",audit_reference="native-lineage/correction",checked_at="2026-10-25T18:00:00Z",
            evidence=[{"id":"same-event","source":_scope(value["plan"]["participants"][0]),"text":body,"excerpt":"World Climate Summit 2027","reason":"Both retained registration representations identify the same edition."}])
        return value
    proposal=manifest();scope=_scope(proposal["plan"]["participants"][1])
    proposal["aliases"]=[{"source":scope,"same_object":True,"evidence_ids":["same-event"]}]
    prepared=prepare_owner_checks(database,[proposal],root=tmp_path/"runs")
    assert prepared["ready"] == [proposal] and not prepared["held"] and not prepared["invalid"]
    staged=_ingest(database,tmp_path,proposal)
    reader=_reader(database,tmp_path)
    assert len(reader.meetings_all(base_date="1900-01-01")) == 2
    _approve(database,tmp_path,staged)
    def check(current, *, updated=False):
        records=current.meetings_all(base_date="1900-01-01")
        assert len(records) == 1
        event=records[0]
        assert event["event_id"] == target and event["online_url"] == registrations[0]
        assert event["start_date"] == ("2027-07-01" if updated else "2027-06-10")
        assert {row["event_source_id"] for row in event["sources"]} >= {row["event_source_id"] for row in source_rows}
        assert {row["source_url"] for row in event["sources"]} == set(urls)
        assert {(row["event_id"],row["record_version"],row["state_sha256"]) for row in event["version_history"]} >= original_versions
        mapping=event["identity_mapping"]
        assert mapping["canonical"] == {"entity_kind":"meeting","entity_id":target}
        assert mapping["approved_snapshot_sha256"] == staged["candidate_sha256"]
        assert mapping["aliases"][0]["source"] == scope and mapping["review_id"]
        assert all(row["content_sha256"] and row["event_id"] in {target,source} for row in event["sources"])
        encoded=json.dumps(event)
        assert all(private not in encoded for private in ("PRIVATE local archive marker","state_json","candidate_json","markdown_content"))
        return event
    def check_modes(*,updated=False):
        for direct in (True,False):
            with reader.public_snapshot(direct=direct):check(reader,updated=updated)
        assert not hasattr(reader,"_meeting_records_cache")
        root=tmp_path/("native-export-updated" if updated else "native-export");(root/"wiki").mkdir(parents=True)
        export_public_snapshot(database,root/"wiki/public-registry.json")
        artifact=json.loads((root/"wiki/public-registry.json").read_text())
        assert source in json.dumps(artifact)
        check(RegistryReader.from_public_export(root),updated=updated)
        with sqlite3.connect(database) as db:
            view=published_snapshots(db,("meeting",))[("meeting",target)]
            assert {row["event_source_id"] for row in view["tables"]["climate_event_sources"]} >= {row["event_source_id"] for row in source_rows}
            assert {row["content_version_id"] for row in view["dependencies"]["article_content_versions"]} >= {row["content_version_id"] for row in source_rows}
            assert {row["meeting_run_id"] for row in view["dependencies"]["meeting_runs"]} >= {row["meeting_run_id"] for row in source_rows}
            assert {row["event_source_id"] for row in view["tables"]["meeting_check_attempts"] if row.get("event_source_id")} >= {row["event_source_id"] for row in source_rows}
    check_modes()
    replacement=("Example confirms World Climate Summit 2027 is rescheduled for July 1–2, 2027 in New York. "
        "Registration deadline May 1, 2027. Register https://example.com/register. Climate risk agenda.")
    replacement_html=("<html><body><p>"+replacement+"</p></body></html>").encode()
    stamp="2026-02-02T08:00:00Z"
    incoming=_stored_item(urls[0],replacement_html.decode(),stamp)
    incoming["evidence"]["content_type"]="text/html"
    store_acquisition_batch(database,_stored_batch("native-later","2026-02-02",stamp,incoming))
    with sqlite3.connect(database) as db:
        acquired=db.execute("SELECT article_id,content_version_id,fetch_id FROM acquisition_items WHERE batch_id='native-later'").fetchone()
        assert acquired[0] == "article-1" and acquired[1] != "content-1"
    # Public capture writer selects the actual newly fetched body; acquisition
    # alone deliberately cannot grant that current-body authority.
    from climate_registry.capture import capture_enrich_registry
    from test_climate_registry_capture import FakeTransport,_response,PUBLIC_V4
    captured=capture_enrich_registry(database,tmp_path/"backups",article_ids=["article-1"],refresh=True,
        resolver=lambda host,port:[PUBLIC_V4],transport=FakeTransport(_response(body=replacement_html)),
        clock=lambda:"2026-02-02T08:00:00Z")
    assert captured["articles"][0]["article_id"] == "article-1" and captured["articles"][0]["status"] in {"captured","enrichment_failed"},captured
    with sqlite3.connect(database) as db:
        db.row_factory=sqlite3.Row
        current=db.execute("SELECT cv.* FROM articles a JOIN article_content_versions cv ON cv.content_version_id=a.current_content_version_id WHERE a.article_id='article-1'").fetchone()
        assert current["content_sha256"] == hashlib.sha256(replacement_html).hexdigest()
        assert current["markdown_content"] == replacement_html.decode() and current["content_version_id"] == acquired[1]
        assert captured["articles"][0]["content_version"] == "reused"
        fetch=db.execute("SELECT fetch_id FROM article_fetches WHERE article_id='article-1' AND content_version_id=? AND fetch_status='success' ORDER BY rowid DESC LIMIT 1",(current["content_version_id"],)).fetchone()[0]
        assert not db.execute("SELECT 1 FROM acquisition_items WHERE fetch_id=?",(fetch,)).fetchone()
    assert process_batch(database,"native-later",prompt_text="new retained same-edition source",prompt_version="v1",provider="fixture",model="fixture",
        extractor=lambda request:{"events":[_candidate(start_date="2027-07-01",end_date="2027-07-02",raw_time_text="July 1–2, 2027",date_evidence="July 1–2, 2027")]})["status"] == "succeeded"
    check(reader)
    with sqlite3.connect(database) as db:
        seed_verified_articles(db)
        seed_verified_native_meetings(db)
        stage_entities(db)
        # This substantive body needs a real complete-content review. A legacy
        # approval here loses the old body's approved ancestry needed by revoke.
        snapshot=snapshot_entity(db,"article","article-1")
        summary="World Climate Summit 2027 is rescheduled for July 1–2, 2027 in New York; registration deadline is May 1, 2027."
        snapshot["derived_display"]={"summary":summary,"content_update":{
            "schema_version":"article-content-update.v1",
            "content_version_id":current["content_version_id"],
            "body_sha256":hashlib.sha256(current["markdown_content"].encode()).hexdigest(),
            "previous_content_version_id":"content-1","previous_body_sha256":hashlib.sha256(body.encode()).hexdigest(),
            "summary":summary,"summary_sha256":hashlib.sha256(summary.encode()).hexdigest(),
            "summary_content_version_id":current["content_version_id"],"summary_support":"complete",
            "change_note":"The same event is rescheduled from June 10–12 to July 1–2, 2027.",
            "change_evidence":{"basis":"body_comparison",
                "previous_excerpt":"World Climate Summit 2027 on June 10–12, 2027 in New York.",
                "current_excerpt":"World Climate Summit 2027 is rescheduled for July 1–2, 2027 in New York."},
            "first_observed_at":_collection_timestamp(current["first_fetched_at"]),"date_evidence":{}}}
        content_sha=stage_snapshot(db,snapshot,enrichment_scope="complete_article_content_update")
    reviewed=_native_contract_review(database,tmp_path/"content-update-review",monkeypatch,identity="article-1")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='article-1'").fetchone()[0] == content_sha
        assert all(db.execute("SELECT status FROM registry_reviews WHERE review_id=?",(review,)).fetchone()[0] == "pass" for review in reviewed["review_ids"])
        ancestry=_article_dependency(db,_load(tmp_path/"runs",database),"article-1","content-1")
        assert ancestry["approved"] == original_article_sha
    reviewed=_native_contract_review(database,tmp_path/"event-update-review",monkeypatch,identity=target)
    with sqlite3.connect(database) as db:
        assert all(db.execute("SELECT status FROM registry_reviews WHERE review_id=?",(review,)).fetchone()[0] == "pass" for review in reviewed["review_ids"])
    check_modes(updated=True)
    current=check(reader,updated=True)["identity_mapping"]
    revoke=manifest();revoke["alias_operations"]=[{"action":"revoke","alias_sha256":digest(current["aliases"][0]),"evidence_ids":["same-event"]}]
    prepared=prepare_owner_checks(database,[revoke],root=tmp_path/"runs")
    assert prepared["ready"] == [revoke] and not prepared["held"] and not prepared["invalid"]
    _approve(database,tmp_path,_ingest(database,tmp_path,revoke))
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):
            events={row["event_id"]:row for row in reader.meetings_all(base_date="1900-01-01")}
            assert set(events) == {target,source}
            assert {row["source_url"] for row in events[target]["sources"]} == {urls[0]}
            assert {row["event_id"] for row in events[target]["version_history"]} == {target}
            assert not events[target]["identity_mapping"]["aliases"]
            assert events[target]["identity_mapping"]["revoked_aliases"]
    with sqlite3.connect(database) as db:
        for table,rows in originals.items():
            retained=db.execute("SELECT * FROM "+table).fetchall()
            if table != "climate_events":assert all(row in retained for row in rows)
            else:assert {row[0] for row in retained} == {row[0] for row in rows}



@pytest.mark.parametrize("mode",["dynamic","static"])
@pytest.mark.parametrize("duplicate_only",[False,True])
def test_legacy_pdf_report_overlay_preserves_exact_modules_before_pagination(tmp_path,monkeypatch,mode,duplicate_only):
    from fastapi.testclient import TestClient
    from climate_registry.read_api import RegistryReader
    from climate_registry.schema import apply_migrations
    from test_registry_publication import _pdf
    import api_server
    runtime,database,ids=_http_same_url_pdf_reader(tmp_path,monkeypatch,mode,same_parent_observation=duplicate_only)
    selected={"occ-2026","occ-2026-repeat"} if duplicate_only else ids
    expected={"pdf-edition-2026":selected if duplicate_only else {"occ-2026"}}
    if not duplicate_only:expected["pdf-edition-2027"]={"occ-2027"}
    assert {row["article_id"]:{item["occurrence_id"] for item in row["occurrences"]}
        for row in runtime.pdf_report("f"*64,allowed_occurrence_ids=selected)["articles"]} == expected
    public=tmp_path/"legacy-report-public.sqlite3"
    with sqlite3.connect(public) as db:
        apply_migrations(db,target_version=20)
        _pdf(db,identity="pdf-edition-2026",url="https://example.org/good",occurrence_id="occ-2026")
    reader=RegistryReader(public,repository_root=tmp_path/"legacy-report-app")
    monkeypatch.setattr(api_server,"_registry_reader",lambda:reader)
    monkeypatch.setattr(api_server,"_pdf_registry_view",lambda:(runtime,selected,set()))
    api_server._clear_registry_list_cache()
    with sqlite3.connect(database) as db:
        original=db.execute("SELECT * FROM pdf_intake_article_occurrences ORDER BY occurrence_id").fetchall()
    def check(report,identities):
        assert len(report["articles"]) == len(identities)
        assert {row["article_id"]:{item["occurrence_id"] for item in row["occurrences"]}
            for row in report["articles"]} == {identity:expected[identity] for identity in identities}
        assert all(len(row["occurrences"]) == len(expected[row["article_id"]]) and row["canonical_url"] == "https://example.org/good" for row in report["articles"])
    check(api_server._pdf_report("f"*64),set(expected))
    client=TestClient(api_server.app)
    response=client.get("/api/registry/pdf-intake/reports/"+"f"*64)
    assert response.status_code == 200,response.text
    check(response.json(),set(expected))
    for page,identity in enumerate(expected,1):
        for result in (api_server._pdf_report("f"*64,page=page,page_size=1),
                client.get("/api/registry/pdf-intake/reports/"+"f"*64+f"?page={page}&page_size=1")):
            if not isinstance(result,dict):
                assert result.status_code == 200,result.text
                result=result.json()
            assert result["pagination"] == {"page":page,"page_size":1,"total":len(expected),"pages":len(expected)}
            check(result,{identity})
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM pdf_intake_article_occurrences ORDER BY occurrence_id").fetchall() == original
    api_server._clear_registry_list_cache()


def test_management_pdf_report_catalog_selects_same_url_parents_by_id(tmp_path):
    from climate_registry.read_api import RegistryReader
    from test_registry_publication import _pdf
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-2026",url="https://example.org/shared",occurrence_id="occ-2026")
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES('pdf-2027','https://example.org/shared','2027 report','2026-09-01')")
        second=list(db.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='occ-2026'").fetchone())
        second[0],second[1]="occ-2027","pdf-2027"
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",second)
        original=db.execute("SELECT * FROM pdf_intake_article_occurrences ORDER BY occurrence_id").fetchall()
    # Management selection tests identity routing, not publication eligibility.
    reader=RegistryReader(database,repository_root=tmp_path/"management-app",public=False)
    catalog=reader.pdf_report_module_catalog("f"*64)
    assert catalog == [("article","pdf_article:pdf-2026"),("article","pdf_article:pdf-2027")]
    for key,identity,occurrence in zip(catalog,("pdf-2026","pdf-2027"),("occ-2026","occ-2027")):
        selected=reader.pdf_report("f"*64,module_keys={key})
        assert [(row["article_id"],[value["occurrence_id"] for value in row["occurrences"]]) for row in selected["articles"]] == [(identity,[occurrence])]
    assert reader.pdf_report("f"*64,module_keys=set())["articles"] == []
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM pdf_intake_article_occurrences ORDER BY occurrence_id").fetchall() == original


@pytest.mark.parametrize("selection",["entity","occurrences"])
def test_legacy_api_overlay_uses_only_reviewed_runtime_occurrence_ownership(tmp_path,monkeypatch,selection):
    from fastapi.testclient import TestClient
    from climate_registry.schema import apply_migrations
    from climate_registry.read_api import RegistryReader
    import api_server
    runtime,database,ids=_http_same_url_pdf_reader(tmp_path,monkeypatch,"dynamic",selection=selection)
    public=tmp_path/"legacy-public.sqlite3"
    with sqlite3.connect(public) as db:apply_migrations(db,target_version=20)
    db.close()
    selected=RegistryReader(public,repository_root=tmp_path/"legacy-application")
    manifest={"web_items":[{"article_id":"good"}],"pdf_occurrence_ids":sorted(ids)}
    monkeypatch.setattr(api_server,"_registry_reader",lambda:selected)
    monkeypatch.setattr(api_server,"_range_report_overlay",lambda:(runtime,runtime,manifest))
    expected=runtime.article("good")
    response=TestClient(api_server.app).get("/api/registry/articles/good")
    assert response.status_code == 200,response.text
    article=response.json()
    assert article["article_id"] == "good" and article["summary"] == expected["summary"]
    assert article["published_candidate_sha256"] == expected["published_candidate_sha256"]
    assert {row["occurrence_id"] for row in article["pdf_occurrences"]} == {"occ-2026"}
    assert not {"occ-2027","occ-remainder"} & {row["occurrence_id"] for row in article["pdf_occurrences"]}
    assert article["identity_mapping"] == expected["identity_mapping"]



@pytest.mark.parametrize("direct",[True,False])
def test_native_meeting_dto_projects_only_safe_approved_lineage_fields(direct):
    from climate_registry.read_api import RegistryReader
    event={"event_id":"target","name":"Reviewed event","event_type":"summit","status":"scheduled",
        "date_precision":"day","start_date":"2027-06-10","end_date":"2027-06-12"}
    sources=[{"event_source_id":"source-"+identity,"event_id":identity,"article_id":"article-"+identity,
        "content_version_id":"body-"+identity,"source_url":"https://example.org/"+identity,
        "content_sha256":"a"*64,"meeting_run_id":"run","candidate_json":"PRIVATE candidate"} for identity in ("target","absorbed")]
    versions=[{"event_id":identity,"record_version":1,"state_sha256":"b"*64,"meeting_run_id":"run",
        "recorded_at":"2026-01-01T00:00:00Z","state_json":"PRIVATE state"} for identity in ("target","absorbed")]
    snapshot={"tables":{"climate_events":[event],"climate_event_sources":sources,"climate_event_versions":versions},
        "dependencies":{"acquisition_batches":[],"article_content_versions":[{"content_version_id":"body-"+identity,"markdown_content":"PRIVATE body"} for identity in ("target","absorbed")]},
        "identity_mapping":{"canonical":{"entity_kind":"meeting","entity_id":"target"},"review_id":"exact-approved-reference"}}
    original=deepcopy(snapshot)
    # This unit tests only the DTO boundary; the real writer/approval sequence is above.
    from contextlib import nullcontext
    connection=sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE public_snapshot_metadata(entity_kind TEXT,entity_id TEXT,snapshot_json TEXT)")
    connection.execute("INSERT INTO public_snapshot_metadata VALUES('meeting','target',?)",(json.dumps(snapshot),))
    reader=object.__new__(RegistryReader);reader.public=True;reader.static_snapshot=None
    reader.connect=lambda:nullcontext(connection)
    reader._source_connection=lambda **kwargs:nullcontext(connection)
    reader._public_candidates=lambda *kinds:{("meeting","target"):snapshot}
    with reader.public_snapshot(direct=direct):
        records,_=reader._approved_meeting_records(base_date="1900-01-01",timezone_name="UTC",include_unknown=False)
    assert not hasattr(reader,"_meeting_records_cache")
    connection.close()
    assert len(records) == 1 and records[0]["start_date"] == event["start_date"]
    assert {row["event_id"] for row in records[0]["sources"]} == {"target","absorbed"}
    assert {row["event_id"] for row in records[0]["version_history"]} == {"target","absorbed"}
    assert records[0]["identity_mapping"] == snapshot["identity_mapping"]
    assert "PRIVATE" not in json.dumps(records) and "state_json" not in json.dumps(records)
    assert snapshot == original
