"""Verified-only public facts and explicitly adopted original PDF summaries."""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from climate_registry.publication import _approve, stage_entities, stage_snapshot, snapshot_entity, set_visibility, public_connection, export_public_snapshot
from climate_registry.read_api import RegistryReader, RegistryNotFoundError, RegistryUnavailableError, RegistryContractError
from climate_registry.schema import apply_migrations
from climate_registry.information_checks import run_checks
from climate_registry.pdf_summary import adopt_pdf_summary
from climate_registry.errors import RegistryInputError

DOC = hashlib.sha256(b"%PDF").hexdigest()
TEXT = "Original executive text.\n  Exact spacing retained.\n采用原文。"


def _database(tmp_path, *, document_metadata=None):
    database = tmp_path / "external" / "registry.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        apply_migrations(db)
        db.execute("INSERT INTO sources VALUES('s','example.org','Publisher','2026-09-01','2026-09-01')")
        for identity in ("good", "unchecked"):
            db.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES(?,?,'s','2026-09-01','2026-09-01')", (identity, 'https://example.org/' + identity))
            db.execute("INSERT INTO article_versions VALUES(?,?,?,?,?,?,?,'2026-09-01','2026-09-01')", ('v-' + identity, identity, 'Title ' + identity, 'Title ' + identity, 'Summary ' + identity, 'fingerprint-' + identity, 'report-title-summary'))
            db.execute("UPDATE articles SET current_version_id=? WHERE article_id=?", ('v-' + identity, identity))
        document = {"executive_summary": TEXT, "executive_summary_sha256": hashlib.sha256(TEXT.encode()).hexdigest(), "pages": [], **(document_metadata or {})}
        db.execute("INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at,date_of_run,original_pdf) VALUES(?, '/evidence/report.pdf','report.pdf','application/pdf',4,?,?, '2026-09-14T00:00:00Z','2026-09-14',?)", (DOC, 'e'*64, json.dumps(document), b'%PDF'))
    return database


def _reader(database, tmp_path):
    return RegistryReader(database, repository_root=tmp_path / 'app')


def _check(database, tmp_path, *, kind='articles', ids=None, failed=False, candidate=None, body=None):
    body = body or 'Title good. Summary good. IAIS Annual Conference 2027. June 10–12, 2027. Source evidence for the original report.'
    def fetch(key, url):
        return {'article_id': key, 'requested_url': url, 'final_url': url, 'status': 'failed' if failed else 'ok',
            'content': body, 'content_ref': None, 'content_hash': hashlib.sha256(body.encode()).hexdigest(), 'failure_reason': 'fixture_source_unavailable' if failed else None}
    return run_checks(database, kind=kind, occurrence_ids=ids, reverify_all=True, backup_dir=tmp_path/'backups', fetcher=fetch,
        verifier=lambda kind, fields, content: {'comparisons': {key: {'status': 'supported'} for key in fields}, 'website_candidate': candidate})


def _approve_all(database):
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):
            _approve(db, sha, {'basis': 'explicit isolated fixture approval'}, status='accepted_legacy')
        from climate_registry.display import refresh_display
        refresh_display(db)


def test_frozen_verified_primary_gate_direct_projection_and_pending(tmp_path):
    database = _database(tmp_path)
    _check(database, tmp_path, ids={'core:good'})
    _approve_all(database)
    reader = _reader(database, tmp_path)
    assert [row['article_id'] for row in reader.articles()['items']] == ['good']
    with pytest.raises(RegistryNotFoundError):
        reader.article('unchecked')
    approved = reader.article('good')
    assert approved['report_summary'] == 'Summary good'
    _check(database, tmp_path, ids={'core:good'}, failed=True)
    assert reader.article('good')['published_candidate_sha256'] == approved['published_candidate_sha256']
    with reader.public_snapshot():
        assert [row['article_id'] for row in reader.articles()['items']] == ['good']
        assert reader.article('good')['report_summary'] == 'Summary good'
    from climate_registry.verification_report import write_verification_report
    receipt = write_verification_report(database, tmp_path/'runs')
    inventory = json.loads(Path(receipt['json_path']).read_text(encoding='utf-8'))
    assert inventory['retained_verified_public_versions'][0]['entity_id'] == 'good'
    failed = [row for row in inventory['rows'] if row['entity_id'] == 'good' and row['version'] == 'latest']
    assert failed and failed[0]['verification_status'] == 'unchecked'
    assert not failed[0]['factual_public'] and failed[0]['entity_has_verified_public_version']
    assert failed[0]['public_role'] == 'hidden_facts'
    assert 'failed_access' in failed[0]['reason_codes']
    assert any(row['entity_id'] == 'unchecked' and row['public_role'] == 'hidden_facts' for row in inventory['rows'])
    assert 'Source webpage access failed' in Path(receipt['human_report_path']).read_text(encoding='utf-8')


def test_original_summary_adoption_exact_text_and_archive_independence(tmp_path):
    database = _database(tmp_path)
    _approve_all(database)
    reader = _reader(database, tmp_path)
    from climate_registry.publication import public_wiki_pages
    assert public_wiki_pages(database)['index.md']=='# Climate Registry\n'
    assert reader.pdf_report(DOC)['executive_summary'] == []
    assert reader.pdf_report(DOC, include_bytes=True)['pdf_bytes'] == b'%PDF'
    sha = hashlib.sha256(TEXT.encode()).hexdigest()
    with pytest.raises(RegistryInputError, match='hash differs'):
        adopt_pdf_summary(database, DOC, '0'*64, actor='owner', reason='explicit adoption', backup_dir=tmp_path/'backups', apply=True)
    result = adopt_pdf_summary(database, DOC, sha, actor='owner', reason='explicit exact original adoption', backup_dir=tmp_path/'backups', apply=True)
    assert result['status'] == 'adopted' and result['display_label'] == 'Adopted from the original PDF'
    with sqlite3.connect(database) as db:
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute("UPDATE pdf_intake_summary_adoptions SET adoption_basis='rewritten'")
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute('DELETE FROM pdf_intake_summary_adoptions')
    assert adopt_pdf_summary(database, DOC, sha, actor='owner', reason='same adoption', backup_dir=tmp_path/'backups', apply=True)['status'] == 'no-op'
    payload = reader.pdf_report(DOC, page=1)
    assert payload['executive_summary'] == [TEXT]
    assert payload['executive_summary_label'] == 'Adopted from the original PDF'
    assert reader.pdf_report(DOC, page=2)['executive_summary'] == []
    with reader.public_snapshot():
        assert reader.pdf_report(DOC)['executive_summary'] == [TEXT]
    from climate_registry.publication import public_wiki_pages, export_public_snapshot
    pages = public_wiki_pages(database, reader=reader)
    page = pages['registry-pdf-report-' + DOC + '.md']
    assert TEXT in page and '/pdf' in page
    assert '## Executive Summary\n' in page and '## Executive Summary ·' not in page
    assert page.index(TEXT) < page.index('Note: Adopted from the original PDF')
    root = tmp_path/'static'; (root/'wiki').mkdir(parents=True)
    export_public_snapshot(database, root/'wiki/public-registry.json', reader=reader)
    static = RegistryReader.from_public_export(root)
    assert static.pdf_report(DOC)['executive_summary'] == [TEXT]
    assert static.pdf_report(DOC, page=2)['executive_summary'] == []
    artifact = json.loads((root/'wiki/public-registry.json').read_text(encoding='utf-8'))
    artifact.pop('publication_policy')
    from climate_registry.acquisition_review import digest
    artifact.pop('artifact_sha256'); artifact['artifact_sha256'] = digest(artifact)
    (root/'wiki/public-registry.json').write_text(json.dumps(artifact), encoding='utf-8')
    with pytest.raises((RegistryUnavailableError, RegistryContractError)):
        RegistryReader.from_public_export(root)


@pytest.mark.parametrize("static_label", ["采用原文", "Adopted from the original PDF"])
def test_legacy_original_adoption_receipts_keep_ids_and_normalize_public_labels(tmp_path, static_label):
    from climate_registry.acquisition_review import digest
    from climate_registry.publication import export_public_snapshot, public_wiki_pages
    database = _database(tmp_path)
    documents = [DOC, '1'*64, '2'*64]
    expected = {}
    with sqlite3.connect(database) as db:
        for identity in documents:
            if identity != DOC:
                db.execute("INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at,date_of_run,original_pdf) SELECT ?,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at,date_of_run,original_pdf FROM pdf_intake_documents WHERE document_sha256=?", (identity, DOC))
            receipt = {"document_sha256": identity, "executive_summary_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
                "adopted_at": "2026-10-15T00:00:00Z", "actor": "owner", "authorization_reason": "exact original adoption",
                "display_label": "采用原文", "basis": "adopted_original_pdf"}
            adoption_id = digest(receipt)
            raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
            expected[identity] = (adoption_id, raw)
            db.execute("INSERT INTO pdf_intake_summary_adoptions VALUES(?,?,?,?,?,?)", (adoption_id, identity,
                receipt['executive_summary_sha256'], receipt['adopted_at'], receipt['basis'], raw))
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    reader = _reader(database, tmp_path)
    def verify(current):
        for identity in documents:
            report = current.pdf_report(identity)
            assert report['executive_summary'] == [TEXT]
            assert report['executive_summary_label'] == 'Adopted from the original PDF'
            assert report['executive_summary_reference']['adoption_id'] == expected[identity][0]
    verify(reader)
    with reader.public_snapshot():
        verify(reader)
    pages = public_wiki_pages(database, reader=reader)
    for identity in documents:
        page = pages['registry-pdf-report-' + identity + '.md']
        assert '## Executive Summary\n' in page and '## Executive Summary ·' not in page
        assert page.index(TEXT) < page.index('Note: Adopted from the original PDF')
    root = tmp_path/'static'; (root/'wiki').mkdir(parents=True)
    output = root/'wiki/public-registry.json'
    export_public_snapshot(database, output, reader=reader)
    artifact = json.loads(output.read_text(encoding='utf-8'))
    for payload in artifact['pdf_report_details'].values():
        payload['executive_summary_label'] = static_label
    artifact.pop('artifact_sha256'); artifact['artifact_sha256'] = digest(artifact)
    output.write_text(json.dumps(artifact), encoding='utf-8')
    verify(RegistryReader.from_public_export(root))
    from climate_registry.verification_report import write_verification_report
    report = write_verification_report(database, tmp_path/'runs')
    inventory = json.loads(Path(report['json_path']).read_text(encoding='utf-8'))
    assert {row['adoption_id'] for row in inventory['original_summary_adoptions']} == {row[0] for row in expected.values()}
    assert all(row['label'] == 'Adopted from the original PDF' for row in inventory['original_summary_adoptions'])
    with sqlite3.connect(database) as db:
        assert {row[0]:(row[1],row[2]) for row in db.execute('SELECT document_sha256,adoption_id,receipt_json FROM pdf_intake_summary_adoptions')} == expected
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_t10_peek_emits_report_without_claiming_review(tmp_path, capsys):
    database = _database(tmp_path)
    _approve_all(database)
    from scripts.review_pipeline import main
    registry_before = database.read_bytes()
    assert main(['peek', '--kind', 'acquisition', '--root', str(tmp_path/'runs'), '--database', str(database)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert not result['wakeAgent']
    assert result['verification_report']['status'] == 'written'
    assert database.read_bytes() == registry_before
    assert not list((tmp_path/'runs').rglob('state.json'))
    assert not list((tmp_path/'runs').rglob('packet.json'))
    human = Path(result['verification_report']['human_report_path']).read_text(encoding='utf-8')
    assert human.startswith('# T10 verification and publication inventory')
    assert not any('\u4e00' <= char <= '\u9fff' for char in human)
    inventory = json.loads(Path(result['verification_report']['json_path']).read_text(encoding='utf-8'))
    assert inventory['language'] == 'en'
    from climate_registry.verification_report import REASONS, reason_explanation
    assert all(text.isascii() for text in REASONS.values())
    for code in ('field_outside_verification:summary', 'manual_field_outside_reviewed_scope:summary',
                 'manual_date_outside_evidence:start_date', 'manual_field_value_mismatch:location'):
        assert reason_explanation(code).isascii()
    assert result['verification_report']['report_sha256'][:12] in Path(result['verification_report']['json_path']).name
    directory = Path(result['verification_report']['json_path']).parent
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    assert main(['peek', '--kind', 'acquisition', '--root', str(tmp_path/'runs'), '--database', str(database)]) == 0
    unchanged = json.loads(capsys.readouterr().out)['verification_report']
    assert unchanged['reused'] and unchanged['report_sha256'] == result['verification_report']['report_sha256']
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    set_visibility(database, 'article', 'good', False)
    assert main(['peek', '--kind', 'acquisition', '--root', str(tmp_path/'runs'), '--database', str(database)]) == 0
    changed = json.loads(capsys.readouterr().out)['verification_report']
    assert not changed['reused'] and changed['report_sha256'] != unchanged['report_sha256']
    assert len(list(directory.glob('*.md'))) == 2


def test_t10_rebuilds_cached_legacy_language_report(tmp_path):
    from climate_registry.acquisition_review import digest
    from climate_registry.verification_report import write_verification_report
    database = _database(tmp_path)
    root = tmp_path/'runs'
    receipt = write_verification_report(database, root)
    json_path, human_path = Path(receipt['json_path']), Path(receipt['human_report_path'])
    old = json.loads(json_path.read_text(encoding='utf-8'))
    old.pop('report_sha256'); old.pop('language')
    receipt['semantic_sha256'] = digest({key:value for key,value in old.items() if key != 'generated_at'})
    receipt['report_sha256'] = digest(old)
    json_path.write_text(json.dumps({**old, 'report_sha256':receipt['report_sha256']}), encoding='utf-8')
    human_path.write_text('# T10 核验与公开清单\n', encoding='utf-8')
    receipt['human_report_sha256'] = hashlib.sha256(human_path.read_bytes()).hexdigest()
    (json_path.parent/'latest.json').write_text(json.dumps(receipt), encoding='utf-8')
    updated = write_verification_report(database, root)
    assert not updated['reused'] and updated['report_sha256'] != receipt['report_sha256']
    assert Path(updated['human_report_path']).read_text(encoding='utf-8').startswith('# T10 verification and publication inventory')
    assert human_path.read_text(encoding='utf-8') == '# T10 核验与公开清单\n'  # Historical output is retained.


def _pdf_pair(database, *, target_extra=None, verified_source=False):
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at,date_of_run) VALUES(?, '/evidence/second.pdf','second.pdf','application/pdf',1,?,'{}','2026-09-28','2026-09-28')", ('a'*64, 'b'*64))
        for event, occurrence, document, page, raw_date, summary in (
                ('target', 'target-occ', DOC, 9, 'June 10–12, 2027', 'Verified primary observation'),
                ('source', 'source-occ', 'a'*64, 12, 'June 10–12, 2027' if verified_source else 'Wrong old date',
                    'Verified secondary observation' if verified_source else 'PRIVATE unchecked source summary')):
            item = {'event_id': event, 'occurrence_id': occurrence, 'name': 'IAIS Annual Conference 2027',
                'kind': 'event', 'raw_date': raw_date, 'date_precision': 'day', 'start_date': '2027-06-10',
                'end_date': '2027-06-12', 'summary': summary, 'page': page, 'source_document_sha256': document,
                'source_urls': ['https://example.org/' + event]}
            if event == 'source' and not verified_source:
                item.update(location='PRIVATE old venue', deadline_date='2099-01-01')
            elif target_extra:
                item.update(target_extra)
            db.execute("INSERT INTO pdf_intake_calendar_items VALUES(?,?,?,?,?,'event',?,'day',?,?,?, ?,NULL,?)",
                (occurrence,event,document,page,item['name'],raw_date,item['start_date'],item['end_date'],item['summary'],'c'*64,json.dumps(item)))
    return {'name': 'IAIS Annual Conference 2027', 'event_type': 'conference', 'organizer': None,
        'status': 'scheduled', 'date_precision': 'day', 'start_date': '2027-06-10', 'end_date': '2027-06-12',
        'raw_time_text': None, 'timezone': None, 'location': None, 'online_url': None, 'deadline_type': None,
        'deadline_date': None, 'date_evidence': 'June 10–12, 2027', 'deadline_evidence': None,
        'status_evidence': None, 'relevance_reason': None}


def test_approved_alias_keeps_only_metadata_and_verified_primary_every_public_path(tmp_path):
    database = _database(tmp_path)
    candidate = _pdf_pair(database)
    checked = _check(database, tmp_path, kind='meetings', ids={'target-occ'}, candidate=candidate)
    assert checked['unverified_count'] == 0
    _approve_all(database)
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, 'pdf_meeting', 'source')
        snapshot['derived_display'] = {'canonical_pdf_event_id': 'target'}
        sha = stage_snapshot(db, snapshot)
        _approve(db, sha, {'basis': 'explicit owner reviewed report association'}, status='accepted_legacy')
        raw_before = db.execute('SELECT item_json FROM pdf_intake_calendar_items ORDER BY occurrence_id').fetchall()
    reader = _reader(database, tmp_path)
    def verify(current):
        rows = current.meetings_all(base_date='2026-10-15')
        assert len(rows) == 1 and rows[0]['verification_status'] == 'verified'
        assert rows[0]['start_date'] == '2027-06-10'
        observations = {row['occurrence_id']: row for row in rows[0]['pdf_observations']}
        assert set(observations) == {'target-occ', 'source-occ'}
        assert observations['source-occ']['metadata_only']
        encoded = json.dumps(rows)
        assert 'PRIVATE' not in encoded and 'Wrong old date' not in encoded
        report = current.pdf_report('a'*64)
        assert report['calendar_count'] == 1
        reference = report['calendar_items'][0]
        assert reference['metadata_only'] and reference['page'] == 12
        assert reference['source_document_sha256'] == 'a'*64
        assert 'PRIVATE' not in json.dumps(reference)
    verify(reader)
    with reader.public_snapshot():
        verify(reader)
    from climate_registry.publication import export_public_snapshot, public_wiki_pages
    pages = public_wiki_pages(database, reader=reader)
    assert 'PRIVATE' not in json.dumps(pages)
    assert 'a'*64 in pages['registry-meetings.md'] and DOC in pages['registry-meetings.md']
    assert 'Report reference to verified meeting: IAIS Annual Conference 2027; this report observation is unverified.' in pages['registry-meetings.md']
    assert 'PDF observation: \n' not in pages['registry-meetings.md']
    root = tmp_path/'static'; (root/'wiki').mkdir(parents=True)
    output = root/'wiki/public-registry.json'
    export_public_snapshot(database, output, reader=reader)
    assert 'PRIVATE' not in output.read_text(encoding='utf-8')
    verify(RegistryReader.from_public_export(root))
    from climate_registry.range_reports import _range_source
    source = _range_source(reader, '2026-09-01', '2026-09-30')
    assert 'PRIVATE' not in json.dumps(source)
    set_visibility(database, 'pdf_meeting', 'target', False)
    assert reader.meetings_all(base_date='1900-01-01') == []
    assert reader.pdf_report('a'*64)['calendar_items'] == []
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT item_json FROM pdf_intake_calendar_items ORDER BY occurrence_id').fetchall() == raw_before


@pytest.mark.skipif(__import__('os').name == 'nt', reason='API collection requires existing POSIX fcntl')
def test_api_pdf_report_and_article_use_verified_gate_and_original_adoption(tmp_path, monkeypatch):
    import api_server
    from fastapi.testclient import TestClient
    database = _database(tmp_path)
    candidate = _pdf_pair(database)
    _check(database, tmp_path, kind='meetings', ids={'target-occ'}, candidate=candidate)
    _approve_all(database)
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, 'pdf_meeting', 'source')
        snapshot['derived_display'] = {'canonical_pdf_event_id': 'target'}
        sha = stage_snapshot(db, snapshot)
        _approve(db, sha, {'basis': 'approved association fixture'}, status='accepted_legacy')
    adopt_pdf_summary(database, DOC, hashlib.sha256(TEXT.encode()).hexdigest(), actor='owner', reason='explicit original adoption', backup_dir=tmp_path/'backups', apply=True)
    monkeypatch.setattr(api_server, '_registry_reader', lambda: _reader(database, tmp_path))
    monkeypatch.setattr(api_server, '_range_report_overlay', lambda: (None, None, None))
    client = TestClient(api_server.app)
    report = client.get('/api/registry/pdf-intake/reports/' + DOC).json()
    assert report['executive_summary'] == [TEXT] and report['executive_summary_label'] == 'Adopted from the original PDF'
    assert client.get('/api/registry/articles/unchecked').status_code == 404
    report = client.get('/api/registry/pdf-intake/reports/' + 'a'*64).json()
    assert report['calendar_count'] == 1 and report['calendar_items'][0]['metadata_only']
    paged = client.get('/api/registry/pdf-intake/reports/' + 'a'*64 + '?page=1&page_size=5').json()
    reference = paged['calendar_items'][0]
    assert reference['metadata_only'] and reference['page'] == 12
    assert reference['verified_primary_display']['name'] == 'IAIS Annual Conference 2027'
    assert reference['verified_primary_display']['source_url'] == 'https://example.org/target'
    assert not reference.get('name') and not reference.get('raw_date')
    assert 'PRIVATE' not in json.dumps(paged)
    assert 'PRIVATE' not in json.dumps(report)


@pytest.mark.skipif(__import__('os').name == 'nt', reason='Shared native fixture imports existing POSIX API management')
def test_native_verified_primary_strips_unchecked_source_evidence(tmp_path):
    from test_issue136_meetings import _database as native_database, _candidate
    from climate_monitor.meetings import process_batch
    from climate_registry.publication import migrate_publication
    first = 'World Climate Summit 2027. June 10–12, 2027. Climate risk agenda.'
    second = first + ' World Climate Summit 2027 meets June 10–12, 2027 at PRIVATE venue.'
    database = native_database(tmp_path, [first, second])
    migrate_publication(database, tmp_path/'backups', apply=True)
    candidate = _candidate(location=None, online_url=None, organizer=None, raw_time_text=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None)
    result = process_batch(database, 'batch', prompt_text='fixture', prompt_version='v1', provider='fixture', model='fixture',
        extractor=lambda request: {'events': [dict(candidate, date_evidence='World Climate Summit 2027 meets June 10–12, 2027 at PRIVATE venue' if 'PRIVATE' in request['article_body'] else candidate['date_evidence'])]})
    assert result['status'] == 'succeeded', result
    with sqlite3.connect(database) as db:
        source = db.execute("SELECT event_source_id FROM climate_event_sources WHERE source_url='https://example.com/1'").fetchone()[0]
    body = first
    run_checks(database, kind='meetings', occurrence_ids={source}, backup_dir=tmp_path/'backups',
        fetcher=lambda key, url: {'article_id': key, 'requested_url': url, 'final_url': url, 'status': 'ok', 'content': body,
            'content_ref': None, 'content_hash': hashlib.sha256(body.encode()).hexdigest()},
        verifier=lambda kind, fields, content: {'comparisons': {key: {'status': 'supported'} for key in fields}, 'website_candidate': candidate})
    _approve_all(database)
    reader = _reader(database, tmp_path)
    for projected in (False, True):
        if projected:
            with reader.public_snapshot():
                rows = reader.meetings_all(base_date='2026-10-15')
        else:
            rows = reader.meetings_all(base_date='2026-10-15')
        assert len(rows) == 1 and rows[0]['verification_status'] == 'verified'
        assert len(rows[0]['sources']) == 2
        assert 'PRIVATE' not in json.dumps(rows)


def test_legacy_explicit_metadata_reference_uses_verified_canonical_title_without_alias_facts(tmp_path):
    database = _database(tmp_path,document_metadata={"legacy_explicit_identity_evidence": {
        "occurrence_id":"article-ref","core_article_id":"good",
        "source_statement":"This retained source explicitly identifies core Article good (Title good).",
        "approval_basis":"historical explicit-link source frozen before the current approval"}})
    _check(database, tmp_path, ids={'core:good'})
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES('pdf-reference','https://example.org/good','PRIVATE alias title','2026-09-14')")
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES('article-ref','pdf-reference',?,7,'https://example.org/good','2026-09-14',NULL,?,?,?)",
            (DOC, 'd'*64, 'c'*64, json.dumps({'summary': 'PRIVATE unchecked alias paragraph', 'summary_basis': 'verbatim_pdf_article_row'})))
        # A retained historical explicit reference, not a new URL-only identity decision.
        db.execute("UPDATE pdf_intake_articles SET core_article_id='good',confirmation_basis='exact_url_eligible_detail' WHERE article_id='pdf-reference'")
    _approve_all(database)
    with sqlite3.connect(database) as db:
        frozen=json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=(SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good')").fetchone()[0])
        assert frozen["tables"]["pdf_intake_articles"][0]["core_article_id"] == "good"
        assert frozen["dependencies"]["pdf_intake_documents"][0]["document_json"].find("legacy_explicit_identity_evidence") >= 0
    reader = _reader(database, tmp_path)
    for projected in (False, True):
        if projected:
            with reader.public_snapshot():
                article = reader.pdf_report(DOC)['articles'][0]
        else:
            article = reader.pdf_report(DOC)['articles'][0]
        assert article['metadata_only'] and article['title'] == 'Title good'
        assert article['verified_primary_reference']['entity_id'] == 'good'
        assert article['occurrences'][0]['page'] == 7
        assert 'PRIVATE' not in json.dumps(article)
    from climate_registry.verification_report import write_verification_report
    # A new identity without any publication is pending, not owner-hidden.
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES('new','https://example.org/new','s','2026-09-28','2026-09-28')")
    receipt = write_verification_report(database, tmp_path/'runs')
    rows = json.loads(Path(receipt['json_path']).read_text(encoding='utf-8'))['rows']
    new = next(row for row in rows if row['entity_id'] == 'new')
    assert 'no_publication' in new['reason_codes'] and 'owner_hidden' not in new['reason_codes']
    assert not new['factual_public']


@pytest.mark.parametrize('primary',['pdf','native'])
def test_pdf_verified_primary_does_not_publish_unchecked_native_dates(tmp_path, monkeypatch, primary):
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        evidence={"text":"UNVERIFIED native date claim"}
        db.execute("INSERT INTO article_date_observations VALUES('native-date','good','https://example.org/good','page_information','2025-01-02','fixture','old','dates','old',?,'2026-09-01')",(json.dumps(evidence),))
        reviewed_date={"date_kind":"updated","date_precision":"day","review_status":"pass",
            "source_url":"https://example.org/good","target_url":"https://example.org/good",
            "field":"Last updated","text":"Last updated 15 October 2026"}
        db.execute("INSERT INTO article_date_observations VALUES('reviewed-update','good','https://example.org/good','page_information','2026-10-15','official_page','source_page','official_dates','reviewed-update',?,'2026-10-16')",(json.dumps(reviewed_date),))
        db.execute("INSERT INTO acquisition_batches VALUES('date-batch','pre-report-acquisition-batch.v1','2026-09-14','2026-09-14T00:00:00Z','2026-09-14T00:01:00Z','{}','no_search','fixture',?,'2026-09-14T00:01:00Z')",('a'*64,))
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,fetched_at,fetch_status,error_code) VALUES('native-fetch','good','https://example.org/good','2026-09-14T00:00:00Z','failed','unavailable')")
        db.execute("INSERT INTO acquisition_items(acquisition_item_id,batch_id,ordinal,article_id,raw_url,source_name,title,summary,discovered_at,discovery_kind,discovery_ref,origins_json,publication_date,publication_date_evidence_json,date_status,selection_status,selection_reason,update_status,material_status,fetch_id,attempts_json,processing_status) VALUES('native-acq','date-batch',1,'good','https://example.org/good','fixture','UNVERIFIED native acquisition title','UNVERIFIED native acquisition summary','2026-09-14T00:00:00Z','site','fixture','[]','2025-01-03',?,'eligible','unselected','failed','failed','error','native-fetch','[]','failed')",(json.dumps(evidence),))
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at,core_article_id) VALUES('linked','https://example.org/good','Verified PDF title','2026-09-14','good')")
        raw={"title":"Verified PDF title","summary":"Verified PDF summary","publication_date":"2026-09-12","publication_date_evidence":"12 September 2026","report_date":"2026-09-14","source_document_sha256":DOC,"page":7,"raw_url":"https://example.org/good","summary_basis":"verbatim_pdf_article_row"}
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES('linked-occ','linked',?,7,'https://example.org/good','2026-09-14','2026-09-12',?,?,?)",(DOC,'d'*64,'c'*64,json.dumps(raw)))
    _check(database,tmp_path,ids={'linked-occ'} if primary=='pdf' else {'core:good'})
    _approve_all(database)
    before=hashlib.sha256(database.read_bytes()).hexdigest()
    reader=_reader(database,tmp_path)
    def verify(current):
        detail=current.article('good')
        assert detail['title']==('Verified PDF title' if primary=='pdf' else 'Title good')
        assert detail['summary']==('Verified PDF summary' if primary=='pdf' else 'Summary good')
        assert [item['observed_at'] for item in detail['date_observations']] == ['2026-10-15']
        assert detail['updated_date'] == '2026-10-15' and detail['information_date'] is None
        assert 'UNVERIFIED' not in json.dumps(detail)
        assert all(item.get('publication_date') is None and item.get('publication_date_evidence') is None for item in detail['acquisition_observations'])
        assert detail['pdf_occurrences'][0]['publication_date']==('2026-09-12' if primary=='pdf' else None)
        if primary=='native':assert detail['publication_date'] is None
    verify(reader)
    with reader.public_snapshot():verify(reader)
    root=tmp_path/'static';(root/'wiki').mkdir(parents=True)
    export_public_snapshot(database,root/'wiki/public-registry.json')
    verify(RegistryReader.from_public_export(root))
    from climate_registry.range_reports import freeze_range_report
    result=freeze_range_report(reader,tmp_path/'range',start_date='2026-09-01',end_date='2026-09-30')
    assert 'UNVERIFIED' not in json.dumps(result)
    assert '2025-01-02' not in json.dumps(result) and '2025-01-03' not in json.dumps(result)
    for selected,label in ((reader,'direct'),(RegistryReader.from_public_export(root),'static')):
        old=freeze_range_report(selected,tmp_path/('old-range-'+label),start_date='2025-01-01',end_date='2025-01-31')
        assert old['articles']==[] and old['pdf_source_updates']==[]
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    from agentic_wiki.wiki_agent import WikiKnowledgeBase
    kb=WikiKnowledgeBase(wiki_dir=root/'wiki',source_dir=root/'sources')
    assert any(('Verified PDF summary' if primary=='pdf' else 'Summary good') in document.markdown for document in kb.documents)
    assert not any('UNVERIFIED' in document.markdown for document in kb.documents)
    assert hashlib.sha256(database.read_bytes()).hexdigest()==before


def test_native_verified_summary_does_not_publish_unchecked_generated_facts(tmp_path, monkeypatch):
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        body="Title good. Summary good. Published 12 September 2026."
        body_sha=hashlib.sha256(body.encode()).hexdigest()
        db.execute("INSERT INTO article_content_versions VALUES('body-good','good',?,?,?,'text/markdown',?,'fixture','v1','2026-09-01')",(body_sha,body,body_sha,len(body.encode())))
        db.execute("UPDATE articles SET current_content_version_id='body-good' WHERE article_id='good'")
        db.execute("INSERT INTO acquisition_batches VALUES('verified-batch','pre-report-acquisition-batch.v1','2026-09-14','2026-09-14T00:00:00Z','2026-09-14T00:01:00Z','{}','no_search','fixture',?,'2026-09-14T00:01:00Z')",('a'*64,))
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_type,content_version_id) VALUES('verified-fetch','good','https://example.org/good','https://example.org/good','2026-09-14T00:00:00Z','success',200,'text/markdown','body-good')")
        evidence={"source_url":"https://example.org/good","text":"Published 12 September 2026"}
        db.execute("INSERT INTO acquisition_items(acquisition_item_id,batch_id,ordinal,article_id,raw_url,source_name,title,summary,discovered_at,discovery_kind,discovery_ref,origins_json,publication_date,publication_date_evidence_json,date_status,selection_status,selection_reason,update_status,material_status,fetch_id,content_version_id,attempts_json,processing_status) VALUES('verified-acq','verified-batch',1,'good','https://example.org/good','fixture','Title good','Summary good','2026-09-14T00:00:00Z','site','fixture','[]','2026-09-12',?,'eligible','selected','fixture','baseline','full_content','verified-fetch','body-good','[]','complete')",(json.dumps(evidence),))
        db.execute("INSERT INTO article_enrichments(enrichment_id,article_id,content_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('unchecked-generation','good','body-good','complete','UNVERIFIED enrichment fact','[\"Climate risk\"]','[\"risk\"]','en','model','fixture','v1','2026-09-01')")
        db.execute("INSERT INTO article_date_observations VALUES('unverified-date','good','https://example.org/good','page_information','2025-01-02','fixture','old','dates','old',?,'2026-09-01')",(json.dumps({'text':'UNVERIFIED native date claim'}),))
    _check(database,tmp_path,ids={'core:good'},body=body)
    _approve_all(database)
    before=hashlib.sha256(database.read_bytes()).hexdigest()
    reader=_reader(database,tmp_path)
    def verify(current):
        detail=current.article('good')
        assert detail['title']=='Title good' and detail['summary']=='Summary good'
        assert detail['publication_date']=='2026-09-12'
        assert detail['date_observations']==[] and detail['information_date'] is None
        assert 'UNVERIFIED' not in json.dumps(detail)
        assert detail['enrichment']['summary']=='Summary good'
        assert detail['categories']==['Climate risk'] and detail['keywords']==['risk']
        assert detail['metadata_provenance']=={'categories':'content_enrichment','keywords':'content_enrichment'}
        assert detail['enrichment']['generator']['name']=='fixture'
    verify(reader)
    with reader.public_snapshot():verify(reader)
    root=tmp_path/'static';(root/'wiki').mkdir(parents=True)
    export_public_snapshot(database,root/'wiki/public-registry.json')
    verify(RegistryReader.from_public_export(root))
    from climate_registry.publication import public_wiki_pages
    wiki=public_wiki_pages(database)
    assert 'Summary good' in wiki['article-good.md'] and 'UNVERIFIED' not in '\n'.join(wiki.values())
    from climate_registry.range_reports import freeze_range_report
    result=freeze_range_report(reader,tmp_path/'range',start_date='2026-09-01',end_date='2026-09-30')
    assert result['articles'][0]['summary']=='Summary good'
    assert 'UNVERIFIED' not in json.dumps(result)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    from agentic_wiki.wiki_agent import WikiKnowledgeBase
    kb=WikiKnowledgeBase(wiki_dir=root/'wiki',source_dir=root/'sources')
    assert any('Summary good' in document.markdown for document in kb.documents)
    assert not any('UNVERIFIED' in document.markdown for document in kb.documents)
    from climate_registry.verification_report import write_verification_report
    receipt=write_verification_report(database,tmp_path/'runs')
    rows=json.loads(Path(receipt['json_path']).read_text(encoding='utf-8'))['rows']
    published=next(row for row in rows if row['version']=='published' and row['occurrence_id']=='core:good')
    assert published['factual_public'] and published['public_role']=='verified_primary'
    assert {'field_outside_verification:enrichment.summary','unverified_date_observation'} <= set(published['reason_codes'])
    assert not any('category' in code or 'keyword' in code for code in published['reason_codes'])
    assert 'additional unverified fields are hidden' in Path(receipt['human_report_path']).read_text(encoding='utf-8')
    assert hashlib.sha256(database.read_bytes()).hexdigest()==before


def test_verified_pdf_meeting_does_not_publish_unchecked_descriptive_text(tmp_path, monkeypatch):
    database=_database(tmp_path)
    marker='UNVERIFIED meeting description claim'
    candidate=_pdf_pair(database,target_extra={'summary':marker,'raw_text':marker,'description':marker,'table_description':marker,
        'publisher':'IAIS','organizer':'IAIS','timezone':'UTC','relevance':'Forward diary item.'})
    candidate.update(organizer='IAIS',timezone='UTC',relevance_reason='Forward diary item.')
    checked=_check(database,tmp_path,kind='meetings',ids={'target-occ'},candidate=candidate,
        body='IAIS Annual Conference 2027 takes place June 10–12, 2027 UTC. Forward diary item.')
    assert checked['unverified_count']==0
    _approve_all(database)
    before=hashlib.sha256(database.read_bytes()).hexdigest()
    from climate_monitor import pdf_intake
    monkeypatch.setattr(pdf_intake,'recover_calendar_fields',lambda *_args:pytest.fail('unverified PDF prose must not be recovered into public facts'))
    reader=_reader(database,tmp_path)
    def verify(current):
        calendar=current.pdf_calendar_items_all()
        meetings=current.meetings_all(base_date='2026-10-15')
        assert len(calendar)==len(meetings)==1
        assert meetings[0]['name']==candidate['name'] and meetings[0]['start_date']=='2027-06-10'
        assert meetings[0]['verification_status']=='verified'
        assert calendar[0]['organizer']=='IAIS' and calendar[0]['event_timezone']=='UTC'
        assert calendar[0]['relevance_reason']=='Forward diary item.'
        assert marker not in json.dumps([calendar,meetings,current.pdf_report(DOC)])
        assert not calendar[0].get('summary') and 'raw_text' not in calendar[0]
    verify(reader)
    with reader.public_snapshot():verify(reader)
    root=tmp_path/'static';(root/'wiki').mkdir(parents=True)
    export_public_snapshot(database,root/'wiki/public-registry.json')
    verify(RegistryReader.from_public_export(root))
    from climate_registry.range_reports import freeze_range_report
    result=freeze_range_report(reader,tmp_path/'range',start_date='2026-09-01',end_date='2026-09-30')
    assert marker not in json.dumps(result)
    from climate_registry.publication import public_wiki_pages
    pages=public_wiki_pages(database,reader=reader)
    assert candidate['name'] in pages['registry-meetings.md'] and marker not in json.dumps(pages)
    monkeypatch.setenv('CLIMATE_REGISTRY_DB',str(database))
    from agentic_wiki.wiki_agent import WikiKnowledgeBase
    kb=WikiKnowledgeBase(wiki_dir=root/'wiki',source_dir=root/'sources')
    assert any(candidate['name'] in document.markdown for document in kb.documents)
    assert not any(marker in document.markdown for document in kb.documents)
    from climate_registry.verification_report import write_verification_report
    receipt=write_verification_report(database,tmp_path/'runs')
    rows=json.loads(Path(receipt['json_path']).read_text(encoding='utf-8'))['rows']
    published=next(row for row in rows if row['version']=='published' and row['occurrence_id']=='target-occ')
    assert published['factual_public'] and published['public_role']=='verified_primary'
    assert {'field_outside_verification:summary','field_outside_verification:raw_text'} <= set(published['reason_codes'])
    assert reader.pdf_report(DOC,include_bytes=True)['pdf_bytes']==b'%PDF'
    assert hashlib.sha256(database.read_bytes()).hexdigest()==before


@pytest.mark.parametrize('policy',['summary_excerpt','full_markdown'])
@pytest.mark.parametrize('same_body',[True,False])
def test_native_body_requires_the_actual_verified_reader_hash(tmp_path, monkeypatch, policy, same_body):
    database=_database(tmp_path)
    reader_body='Title good. Summary good. Current official source body.'
    retained=reader_body if same_body else 'UNVERIFIED old source body claim'
    body_sha=hashlib.sha256(retained.encode()).hexdigest()
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_content_versions VALUES('retained-body','good',?,?,?,'text/markdown',?,'fixture','v1','2026-09-01')",(body_sha,retained,body_sha,len(retained)))
        db.execute("UPDATE articles SET current_content_version_id='retained-body',display_policy=? WHERE article_id='good'",(policy,))
    assert _check(database,tmp_path,ids={'core:good'},body=reader_body)['unverified_count']==0
    _approve_all(database)
    before=hashlib.sha256(database.read_bytes()).hexdigest()
    reader=_reader(database,tmp_path)
    def verify(current):
        detail=current.article('good')
        assert detail['title']=='Title good' and detail['summary']=='Summary good'
        assert detail['content']['content_version_id']=='retained-body'
        assert detail['available_content']['content_version_id']=='retained-body'
        assert ('Current official source body' in json.dumps(detail))==same_body
        assert 'UNVERIFIED' not in json.dumps(detail)
    verify(reader)
    with reader.public_snapshot():verify(reader)
    root=tmp_path/'static';(root/'wiki').mkdir(parents=True)
    export_public_snapshot(database,root/'wiki/public-registry.json')
    verify(RegistryReader.from_public_export(root))
    from climate_registry.range_reports import freeze_range_report
    assert 'UNVERIFIED' not in json.dumps(freeze_range_report(reader,tmp_path/'range',start_date='2026-09-01',end_date='2026-09-30'))
    monkeypatch.setenv('CLIMATE_REGISTRY_DB',str(database))
    from agentic_wiki.wiki_agent import WikiKnowledgeBase
    kb=WikiKnowledgeBase(wiki_dir=root/'wiki',source_dir=root/'sources')
    assert any('Summary good' in document.markdown for document in kb.documents)
    assert not any('UNVERIFIED' in document.markdown for document in kb.documents)
    from climate_registry.verification_report import write_verification_report
    receipt=write_verification_report(database,tmp_path/'runs')
    rows=json.loads(Path(receipt['json_path']).read_text(encoding='utf-8'))['rows']
    published=next(row for row in rows if row['version']=='published' and row['occurrence_id']=='core:good')
    assert ('reader_body_mismatch' in published['reason_codes']) != same_body
    assert hashlib.sha256(database.read_bytes()).hexdigest()==before


def test_verified_pdf_report_fields_do_not_infer_neighbor_publisher(tmp_path, monkeypatch):
    from test_registry_publication import _database as publication_database, _pdf
    from climate_registry.range_reports import _range_source, freeze_range_report, render_range_report_html
    from climate_registry.publication import public_wiki_pages
    title = "WRI verified climate source report"
    summary = title + "\nIN WINDOW 4 AUG 2026 REPORT\nVerified source summary.\nSource: wri.org"
    database = publication_database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db, identity="wri-pdf", url="https://wri.org/verified", period=("2026-08-01", "2026-08-10"),
            occurrence={"title": title, "summary": summary, "summary_basis": "verbatim_pdf_article_row"},
            document={"pages": [{"page": 1, "text": "World Trade Organization (WTO)\nWebsite: https://wto.org", "links": []},
                {"page": 2, "text": summary, "links": [{"url": "https://wri.org/verified", "anchor_text": title}]}]})
    _approve_all(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    reader = _reader(database, tmp_path)
    def verify(current):
        occurrence = current.pdf_article("wri-pdf")["occurrences"][0]
        assert occurrence["report_fields"]["publisher"] is None
        assert occurrence["report_fields"]["title"] == title
        assert occurrence["summary"] == summary
        assert "WTO" not in json.dumps(current.pdf_report("f"*64))
        assert "WTO" not in json.dumps(_range_source(current, "2026-08-01", "2026-08-10"))
    verify(reader)
    with reader.public_snapshot(): verify(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    verify(RegistryReader.from_public_export(root))
    assert "WTO" not in json.dumps(public_wiki_pages(database))
    report = freeze_range_report(reader, tmp_path/"range", start_date="2026-08-01", end_date="2026-08-10")
    assert "WTO" not in render_range_report_html(report)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    from agentic_wiki.wiki_agent import WikiKnowledgeBase
    kb = WikiKnowledgeBase(wiki_dir=root/"wiki", source_dir=root/"sources")
    assert any("Verified source summary" in doc.markdown for doc in kb.documents)
    assert not any("WTO" in doc.markdown for doc in kb.documents)
    from climate_registry.verification_report import write_verification_report
    receipt = write_verification_report(database, tmp_path/"runs")
    rows = json.loads(Path(receipt["json_path"]).read_text())["rows"]
    published = next(row for row in rows if row["version"] == "published" and row["occurrence_id"] == "occurrence")
    assert "field_outside_verification:report_fields.publisher" in published["reason_codes"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("selected_success", [True, False])
def test_available_content_provenance_belongs_to_selected_body_not_latest_fetch(tmp_path, selected_success):
    database = _database(tmp_path)
    body = "Title good. Summary good."
    selected_sha = hashlib.sha256(body.encode()).hexdigest()
    other = "UNVERIFIED unrelated later body"
    other_sha = hashlib.sha256(other.encode()).hexdigest()
    with sqlite3.connect(database) as db:
        for identity, text, sha in (("selected-body", body, selected_sha), ("other-body", other, other_sha)):
            db.execute("INSERT INTO article_content_versions VALUES(?,'good',?,?,?,'text/markdown',?,'fixture','v1','2026-09-01')",
                (identity, sha, text, sha, len(text)))
        db.execute("UPDATE articles SET current_content_version_id='selected-body' WHERE article_id='good'")
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id,error_code) VALUES('selected-fetch','good','https://example.org/good?original=1','https://example.org/good','2026-09-01T12:00:00Z',?,?,?,?)",
            ("success" if selected_success else "failed", 200 if selected_success else 403, "selected-body" if selected_success else None, None if selected_success else "http_error"))
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES('other-fetch','good','https://example.org/unrelated','https://example.org/unrelated-final','2026-09-30T12:00:00Z','success',200,'other-body')")
    _check(database, tmp_path, ids={"core:good"}, body=body)
    _approve_all(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    reader = _reader(database, tmp_path)
    def verify(current):
        detail = current.article("good")
        content = detail["available_content"]
        assert content["content_version_id"] == "selected-body" and content["content_sha256"] == selected_sha
        assert "UNVERIFIED" not in json.dumps(detail)
        if selected_success:
            assert content["fetch_id"] == "selected-fetch"
            assert content["requested_url"] == "https://example.org/good?original=1"
            assert content["final_url"] == "https://example.org/good"
        else:
            assert content["fetch_id"] is None and content["collected_at"] is None
            assert "requested_url" not in content and "final_url" not in content
    verify(reader)
    with reader.public_snapshot(): verify(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    static = RegistryReader.from_public_export(root); verify(static)
    from climate_registry.range_reports import _range_source
    live = _range_source(reader, "2026-09-01", "2026-09-01")
    assert bool(live["articles"]) == selected_success
    assert _range_source(static, "2026-09-01", "2026-09-01")["articles"] == live["articles"]
    for current in (reader, static):
        assert _range_source(current, "2026-09-30", "2026-09-30")["articles"] == []
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("binding", ["exact", "403", "wrong_sha", "wrong_url"])
def test_native_verified_facts_keep_exact_source_report_classification_metadata(tmp_path, monkeypatch, binding):
    from climate_registry.persistent import initialize_registry, update_registry
    from climate_registry.publication import public_wiki_pages
    from climate_registry.range_reports import _range_source
    source = tmp_path/"sources"; source.mkdir()
    report = source/"climate-monitor-2026-09-07.md"
    raw = "# Weekly Climate Monitor\n\n**Report Date:** 2026-09-07\n\n## Executive Summary\n- Sites checked: **1**, succeeded: **1**, failed: **0**\n- Original archive summary.\n\n## Pillar A — Changes\n- **Original source title**\n  - UNVERIFIED report narrative.\n  - **Categories:** Climate Risk\n  - **Keywords:** insurance\n  🔗 https://example.org/classification\n\n## Pillar B — Intelligence\n\n## Original Links\n- https://example.org/classification\n"
    report.write_text(raw, encoding="utf-8")
    database = tmp_path/"registry.sqlite3"; initialize_registry(database)
    update_registry(source, database, tmp_path/"backups")
    with sqlite3.connect(database) as db:
        identity = db.execute("SELECT article_id FROM articles").fetchone()[0]
        db.execute("UPDATE article_versions SET observed_title='Verified title',canonical_title='Verified title',observed_summary='Verified summary'")
        body = "Verified title. Verified summary."
        sha = hashlib.sha256(body.encode()).hexdigest()
        db.execute("INSERT INTO article_content_versions VALUES('checked-body',?,?,?,?, 'text/markdown',?,'fixture','v1','2026-09-07')",
            (identity, sha, body, sha, len(body)))
        db.execute("UPDATE articles SET current_content_version_id='checked-body'")
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES('checked-fetch',?,'https://example.org/classification','https://example.org/classification','2026-09-07T12:00:00Z','success',200,'checked-body')", (identity,))
        if binding == "403":
            db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,error_code) VALUES('capture-403',?,'https://example.org/classification','https://example.org/classification','2026-09-08T12:00:00Z','failed',403,'http_error')", (identity,))
    _check(database, tmp_path, ids={"core:"+identity}, body="Verified title. Verified summary.")
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", identity, source_dir=source)
        if binding == "wrong_sha": snapshot["report_fallbacks"]["2026-09-07"]["sha256"] = "0"*64
        if binding == "wrong_url": snapshot["report_fallbacks"]["2026-09-07"]["articles"][0]["url"] = "https://example.org/unrelated"
        sha = stage_snapshot(db, snapshot)
        _approve(db, sha, {"basis":"explicit verified source-report metadata fixture"}, status="accepted_legacy")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    reader = RegistryReader(database, repository_root=tmp_path/"application", source_dir=source)
    def verify(current):
        article = current.article(identity)
        assert article["summary"] == "Verified summary" and article["summary_provenance"] == "verified_source_evidence"
        expected = binding in {"exact", "403"}
        categories, keywords = (["Climate Risk"], ["insurance"]) if expected else ([], [])
        assert article["categories"] == categories and article["keywords"] == keywords
        assert article["metadata_provenance"] == {"categories":"source_report" if expected else None, "keywords":"source_report" if expected else None}
        assert article["report_metadata"] == {"categories":categories, "keywords":keywords}
        assert "UNVERIFIED" not in json.dumps(article)
        assert current.articles()["items"][0]["categories"] == categories
        if binding == "403" and not current.static_snapshot:
            from scripts.weekly_registry_refresh import _verify_article, _JobError
            _verify_article(article, article_id=identity, target_date="2026-09-07", fallback_article_ids=frozenset({identity}))
            with pytest.raises(_JobError, match="article_detail_incomplete"):
                _verify_article(article, article_id=identity, target_date="2026-09-07", fallback_article_ids=frozenset())
    verify(reader)
    with reader.public_snapshot(): verify(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json", source_dir=source)
    static = RegistryReader.from_public_export(root); verify(static)
    live = _range_source(reader, "2026-09-07", "2026-09-07")
    assert live["articles"] and live["articles"][0]["categories"] == (["Climate Risk"] if binding in {"exact", "403"} else [])
    assert _range_source(static, "2026-09-07", "2026-09-07")["articles"] == live["articles"]
    assert "UNVERIFIED" not in json.dumps(live)
    pages = public_wiki_pages(database, reader=reader)
    assert "UNVERIFIED" not in json.dumps(pages)
    if binding in {"exact", "403"}:
        assert any("Climate Risk" in page and "insurance" in page for page in pages.values())
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    from agentic_wiki.wiki_agent import WikiKnowledgeBase
    kb = WikiKnowledgeBase(wiki_dir=root/"wiki", source_dir=source)
    assert not any("UNVERIFIED" in doc.markdown for doc in kb.documents)
    if binding in {"exact", "403"}:
        from climate_registry.verification_report import write_verification_report
        receipt = write_verification_report(database, tmp_path/"runs")
        rows = json.loads(Path(receipt["json_path"]).read_text())["rows"]
        published = next(row for row in rows if row["version"] == "published" and row["occurrence_id"] == "core:"+identity)
        assert {"field_outside_verification:report_fallbacks.title", "field_outside_verification:report_fallbacks.summary"} <= set(published["reason_codes"])
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before and report.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("original_sorts_last", [True, False])
@pytest.mark.parametrize("latest_current_fetch", [True, False])
def test_resolved_acquisition_body_uses_actual_successful_fetch_across_public_paths(tmp_path, original_sorts_last, latest_current_fetch):
    from climate_registry.range_reports import _range_source
    database = _database(tmp_path)
    body = "Title good. Summary good."
    sha = hashlib.sha256(body.encode()).hexdigest()
    original, resolved = ("f"*64, "a"*64) if original_sorts_last else ("a"*64, "f"*64)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_content_versions VALUES('body','good',?,?,?,'text/markdown',?,'fixture','v1','2026-09-01')", (sha, body, sha, len(body)))
        db.execute("UPDATE articles SET current_content_version_id='body',display_policy='full_markdown' WHERE article_id='good'")
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,error_code) VALUES(?,'good','https://example.org/good?original=1','https://example.org/good','2026-09-01T10:00:00Z','failed',403,'http_error')", (original,))
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES(?,'good','https://example.org/good?resolved=1','https://example.org/good-real','2026-09-01T12:00:00Z','success',200,'body')", (resolved,))
        db.execute("INSERT INTO acquisition_batches VALUES('batch','pre-report-acquisition-batch.v1','2026-09-01','2026-09-01T00:00:00Z','2026-09-01T00:01:00Z','{}','no_search','fixture',?,'2026-09-01T00:01:00Z')", ('a'*64,))
        db.execute("INSERT INTO acquisition_items(acquisition_item_id,batch_id,ordinal,article_id,raw_url,source_name,title,summary,discovered_at,discovery_kind,discovery_ref,origins_json,date_status,selection_status,selection_reason,update_status,material_status,fetch_id,content_version_id,attempts_json,processing_status,resolved_by_fetch_id) VALUES('acq','batch',1,'good','https://example.org/good?original=1','Publisher','Title good','Summary good','2026-09-01T00:00:00Z','site','fixture','[]','unknown_pending_review','selected','fixture','baseline','full_content',?,'body','[]','complete',?)", (original, resolved))
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES('earlier-success','good','https://example.org/good?earlier=1','https://example.org/good-earlier','2026-08-31T12:00:00Z','success',200,'body')")
        db.execute("INSERT INTO acquisition_items(acquisition_item_id,batch_id,ordinal,article_id,raw_url,source_name,title,summary,discovered_at,discovery_kind,discovery_ref,origins_json,date_status,selection_status,selection_reason,update_status,material_status,fetch_id,content_version_id,attempts_json,processing_status) VALUES('earlier-acq','batch',2,'good','https://example.org/good?earlier=1','Publisher','Title good','Summary good','2026-08-31T00:00:00Z','site','fixture','[]','unknown_pending_review','selected','fixture','baseline','full_content','earlier-success','body','[]','complete')")
    if latest_current_fetch:
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES('current-success','good','https://example.org/good?current=1','https://example.org/good-current','2026-09-02T12:00:00Z','success',200,'body')")
    assert _check(database, tmp_path, ids={"core:good"}, body=body)["unverified_count"] == 0
    _approve_all(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    reader = _reader(database, tmp_path)
    def verify(current):
        detail = current.article("good")
        content = detail["available_content"]
        assert content["content_version_id"] == "body" and content["content_sha256"] == sha
        assert content["markdown"] == body and content["fetch_id"] == ("current-success" if latest_current_fetch else resolved)
        assert content["acquisition_item_id"] == (None if latest_current_fetch else "acq")
        assert content["collected_at"] == ("2026-09-02T12:00:00Z" if latest_current_fetch else "2026-09-01T12:00:00Z")
        assert content["requested_url"] == ("https://example.org/good?current=1" if latest_current_fetch else "https://example.org/good?resolved=1")
        assert content["final_url"] == ("https://example.org/good-current" if latest_current_fetch else "https://example.org/good-real")
        observation = next(item for item in detail["acquisition_observations"] if item["acquisition_item_id"] == "acq")
        if not current.static_snapshot:
            assert observation["fetch_id"] == original and observation["fetch"]["status"] == "failed"
            assert observation["resolved_by_fetch_id"] == resolved and observation["resolved_fetch"]["status"] == "success"
    verify(reader)
    with reader.public_snapshot(): verify(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    static = RegistryReader.from_public_export(root); verify(static)
    live = _range_source(reader, "2026-09-01", "2026-09-01")
    selected = live["articles"][0]["provenance"]["collection_time"]["selected"]
    assert selected["observation_id"] == resolved
    assert selected["evidence"] == {"requested_url":"https://example.org/good?resolved=1", "final_url":"https://example.org/good-real"}
    assert _range_source(static, "2026-09-01", "2026-09-01")["articles"] == live["articles"]
    earlier = _range_source(reader, "2026-08-31", "2026-08-31")["articles"]
    assert earlier and earlier[0]["provenance"]["collection_time"]["selected"]["observation_id"] == "earlier-success"
    assert _range_source(static, "2026-08-31", "2026-08-31")["articles"] == earlier
    history = _range_source(reader, "2026-08-31", "2026-09-01")["articles"]
    assert _range_source(static, "2026-08-31", "2026-09-01")["articles"] == history
    expected = {resolved, "earlier-success"} | ({"current-success"} if latest_current_fetch else set())
    assert {item["observation_id"] for item in history[0]["provenance"]["collection_time"]["all"]} == expected
    if latest_current_fetch:
        current_day = _range_source(reader, "2026-09-02", "2026-09-02")["articles"]
        assert current_day and current_day[0]["provenance"]["collection_time"]["selected"]["observation_id"] == "current-success"
        assert _range_source(static, "2026-09-02", "2026-09-02")["articles"] == current_day
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_normal_capture_same_body_collection_history_is_frozen_and_shared(tmp_path):
    from climate_registry.capture import capture_enrich_registry
    from test_climate_registry_capture import FakeTransport, _response, PUBLIC_V4
    from climate_registry.range_reports import _range_source
    database = _database(tmp_path)
    original = b"<html><h1>Title good</h1><p>Summary good. Climate risk and insurance capital, flood modelling and regulatory disclosure.</p></html>"
    other = b"<html><h1>Other body</h1><p>Other climate risk source and insurance evidence.</p></html>"
    def capture(day, response):
        return capture_enrich_registry(database, tmp_path/"capture-backups", article_ids=["good"], refresh=True,
            resolver=lambda host, port:[PUBLIC_V4], transport=FakeTransport(response), clock=lambda:day+"T12:00:00Z")
    for day, response in (("2026-08-31", _response(body=other)), ("2026-09-01", _response(body=original)),
            ("2026-09-02", _response(body=original)), ("2026-09-03", _response(403, b"")), ("2026-09-04", _response(304, b""))):
        capture(day, response)
    with sqlite3.connect(database) as db:
        selected_version, body = db.execute("SELECT c.content_version_id,c.markdown_content FROM articles a JOIN article_content_versions c ON c.content_version_id=a.current_content_version_id WHERE a.article_id='good'").fetchone()
        assert db.execute("SELECT count(*) FROM acquisition_items").fetchone()[0] == 0
        successes = db.execute("SELECT fetch_id,fetched_at FROM article_fetches WHERE article_id='good' AND content_version_id=? AND fetch_status='success' ORDER BY fetched_at", (selected_version,)).fetchall()
        assert len(successes) == 2
        failed_enrichments = db.execute("SELECT enrichment_id,status,summary,error_code,error_message FROM article_enrichments WHERE status='failed' ORDER BY enrichment_id").fetchall()
        assert failed_enrichments and all(item[2] is None and item[3] for item in failed_enrichments)
    db.close()
    assert _check(database, tmp_path, ids={"core:good"}, body=body)["unverified_count"] == 0
    _approve_all(database)
    with sqlite3.connect(database) as db:
        approved_sha = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0]
    db.close()
    # A later real capture stages a draft; the published history must stay frozen.
    capture("2026-09-05", _response(body=original))
    reader = _reader(database, tmp_path)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    def verify(current):
        detail = current.article("good")
        assert detail["published_candidate_sha256"] == approved_sha
        assert detail["summary"] == "Summary good" and detail["summary_provenance"] == "verified_source_evidence"
        if not current.static_snapshot:
            with current.connect() as db:
                assert [tuple(row) for row in db.execute("SELECT enrichment_id,status,summary,error_code,error_message FROM article_enrichments WHERE status='failed' ORDER BY enrichment_id")] == failed_enrichments
        collections = detail["available_content"]["collection_observations"]
        assert [(item["fetch_id"], item["fetched_at"]) for item in collections] == successes
        assert all(item["status"] == "success" and item["content_version_id"] == selected_version for item in collections)
        assert all(item["requested_url"] == item["final_url"] == "https://example.org/good" for item in collections)
    verify(reader)
    with reader.public_snapshot(): verify(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    static = RegistryReader.from_public_export(root); verify(static)
    for day, expected in (("2026-08-31",0), ("2026-09-01",1), ("2026-09-02",1),
            ("2026-09-03",0), ("2026-09-04",0), ("2026-09-05",0)):
        live = _range_source(reader, day, day)["articles"]
        assert len(live) == expected
        assert _range_source(static, day, day)["articles"] == live
    live = _range_source(reader, "2026-08-31", "2026-09-05")["articles"]
    assert _range_source(static, "2026-08-31", "2026-09-05")["articles"] == live
    assert {item["observation_id"] for item in live[0]["provenance"]["collection_time"]["all"]} == {identity for identity, stamp in successes}
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
