import hashlib
import json
import sqlite3
import sys
import types
from datetime import date

from climate_registry.schema import apply_migrations
from climate_registry.acquisition import PublicationDatePolicy, store_acquisition_batch
from climate_registry.persistent import initialize_registry
from climate_registry.read_api import RegistryReader
from agentic_wiki.wiki_agent import WikiKnowledgeBase
from scripts.sync_source_wiki import _render_registry_article, sync_source_wiki


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _registry(path):
    with sqlite3.connect(path) as connection:
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO sources VALUES ('example', 'example.org', 'Example', '2026-09-28', '2026-09-28')"
        )
        connection.execute(
            """INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen,
               current_version_id, document_kind, publication_eligible, current_content_version_id, display_policy)
               VALUES ('article-confirmed', 'https://example.org/confirmed', 'example', '2026-09-28', '2026-09-28',
               'article-version', 'article', 1, NULL, 'full_markdown')"""
        )
        connection.execute(
            """INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen,
               current_version_id, document_kind, publication_eligible, current_content_version_id, display_policy)
               VALUES ('article-pdf-only', 'https://example.org/pdf-only', 'example', '2026-09-28', '2026-09-28',
               NULL, 'article', 1, NULL, 'metadata_only')"""
        )
        connection.execute(
            "INSERT INTO article_versions VALUES ('article-version', 'article-confirmed', 'Confirmed climate article', 'confirmed climate article', 'Report summary for confirmed article.', ?, 'report-title-summary', '2026-09-28', '2026-09-28')",
            (_sha('article-version'),),
        )
        content = '# Confirmed article\n\nConfirmed article body about glacier pricing.'
        connection.execute(
            """INSERT INTO article_content_versions VALUES ('content-v1', 'article-confirmed', ?, ?, ?, 'text/html', 10,
               'html-to-markdown', '1', '2026-09-28T00:00:00Z')""",
            (_sha(content), content, _sha(content + 'markdown')),
        )
        connection.execute("UPDATE articles SET current_content_version_id='content-v1' WHERE article_id='article-confirmed'")
        connection.execute(
            """INSERT INTO article_enrichments VALUES ('enrichment-v1', 'content-v1', 'complete', 'Confirmed semantic summary.',
               '[\"Climate\"]', '[\"glacier pricing\"]', 'en', 'deterministic', 'rules', '1', '2026-09-28T00:00:00Z', NULL, NULL)"""
        )
        connection.execute(
            """INSERT INTO pdf_intake_documents(document_sha256, source_path, filename, media_type, size_bytes,
               extracted_text_sha256, document_json, imported_at)
               VALUES (?, 'C:/reports/evidence.pdf', 'evidence.pdf', 'application/pdf', 1, ?, '{}', '2026-09-28T00:00:00Z')""",
            ('a' * 64, 'b' * 64),
        )
        connection.execute(
            "INSERT INTO pdf_intake_document_sources VALUES (?, 'C:/reports/evidence.pdf', 'evidence.pdf', '2026-09-28T00:00:00Z')",
            ('a' * 64,),
        )
        connection.executemany(
            """INSERT INTO pdf_intake_articles(article_id, canonical_url, title, type_safe_classification_json, imported_at,
               core_article_id, confirmation_basis) VALUES (?, ?, ?, ?, '2026-09-28T00:00:00Z', ?, ?)""",
            (
                ('pdf-confirmed', 'https://example.org/confirmed', 'Confirmed PDF', '{"label":"article"}', 'article-confirmed', 'exact_url_eligible_detail'),
                ('pdf-only-confirmed', 'https://example.org/pdf-only', 'PDF-only confirmed', '{"label":"article"}', 'article-pdf-only', 'exact_url_eligible_detail'),
                ('pdf-unconfirmed', 'https://example.org/home', 'Homepage PDF', '{"label":"landing_page"}', None, None),
            ),
        )
        for article_id, url, summary, page in (
            ('pdf-confirmed', 'https://example.org/confirmed?pdf=1', 'PDF observation for confirmed article.', 3),
            ('pdf-confirmed', 'https://example.org/confirmed?pdf=2', 'Second PDF observation for confirmed article.', 4),
            ('pdf-only-confirmed', 'https://example.org/pdf-only', 'PDF-only confirmed summary.', 5),
            ('pdf-unconfirmed', 'https://example.org/home', 'Unconfirmed PDF observatory signal.', 7),
            ('pdf-unconfirmed', 'https://example.org/home?pdf=2', 'Second unconfirmed PDF observatory signal.', 8),
        ):
            occurrence = {
                'occurrence_id': f'occurrence-{article_id}-{page}', 'raw_url': url, 'page': page,
                'summary': summary, 'source_document_sha256': 'a' * 64,
            }
            connection.execute(
                """INSERT INTO pdf_intake_article_occurrences VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
                (occurrence['occurrence_id'], article_id, 'a' * 64, page, url, 'c' * 64, 'd' * 64, json.dumps(occurrence)),
            )


def _acquisition_item(url, body, *, kind, ref, search_ref=None, discovered_at='2026-09-28T08:00:00Z'):
    digest = _sha(body)
    return {
        'url': url, 'title': 'Acquired glacier pricing', 'summary': f'{kind} summary for {body[-2:]}',
        'source': 'Example Institute', 'discovered_at': discovered_at,
        'discovery_kind': kind, 'discovery_ref': ref, 'discovery_search_ref': search_ref,
        'published_date': '2026-09-27',
        'publication_date_evidence': {'kind': 'publisher', 'url': url, 'text': 'Published 27 September 2026'},
        'selected': True, 'selection_reason': 'relevant', 'processing_status': 'complete', 'processing_error': None,
        'evidence': {
            'status': 'ok', 'fetched_at': discovered_at, 'final_url': url,
            'attempts': [{'engine': 'web_http', 'status': 'success', 'http_status': 200, 'attempted_at': discovered_at}],
            'selected_method': 'web_http', 'content_type': 'text/markdown', 'content': body,
            'content_hash': digest, 'content_ref': 'managed/content.md', 'raw_snapshot_ref': 'managed/raw.html',
            'raw_snapshot_sha256': 'a' * 64, 'classification': 'full_content', 'failure_reason': None,
            'http_status': 200,
        },
    }


def _acquisition_batch(items, *, batch_id, report_date, searches):
    timestamp = f'{report_date}T08:00:00Z'
    policy = PublicationDatePolicy.resolve(None, anchor_date=date.fromisoformat(report_date), frozen_at=timestamp)
    return {
        'schema_version': 'pre-report-acquisition-batch.v1', 'batch_id': batch_id,
        'report_date': report_date, 'started_at': timestamp, 'completed_at': timestamp,
        'date_policy': policy.to_dict(), 'search_decision': (
            {'status': 'attempted', 'reason': None} if searches else {'status': 'no_search', 'reason': 'site observation only'}
        ),
        'searches': searches, 'items': items,
    }


def test_registry_report_summary_is_rendered_once_with_its_report_citation():
    summary = 'Report-derived glacier pricing summary.'
    page = _render_registry_article({
        'article_id': 'article-report-summary',
        'canonical_url': 'https://example.org/report-summary',
        'title': 'Report summary article',
        'content': {}, 'available_content': {}, 'summary': None,
        'current_version_id': 'report-version-1', 'report_summary': summary,
        'acquisition_observations': [], 'pdf_occurrences': [],
        'appearances': [{
            'version_id': 'report-version-1',
            'source_filename': 'climate-monitor-2026-09-28.md',
            'source_sha256': 'f' * 64,
            'original_url': 'https://example.org/original-report-summary',
            'summary': summary,
        }],
    })

    assert page is not None
    assert page.count(summary) == 1
    assert '## Current report summary' not in page
    retrieval_section = page.split('## Report observation: climate-monitor-2026-09-28.md', 1)[1]
    assert summary in retrieval_section
    assert 'Report citation: climate-monitor-2026-09-28.md; SHA-256: ' + 'f' * 64 in retrieval_section
    assert '[original link](https://example.org/original-report-summary)' in retrieval_section


def test_registry_report_summary_without_appearance_keeps_article_version_provenance():
    summary = 'Unattached Registry report summary.'
    page = _render_registry_article({
        'article_id': 'article-summary-only',
        'canonical_url': 'https://example.org/summary-only',
        'title': 'Summary-only article',
        'content': {}, 'available_content': {}, 'summary': None,
        'current_version_id': 'report-version-only', 'report_summary': summary,
        'appearances': [], 'acquisition_observations': [], 'pdf_occurrences': [],
    })

    assert page is not None
    assert '## Registry article-version summary' in page
    assert summary in page
    assert 'Registry article version: report-version-only' in page
    assert 'Article citation: [https://example.org/summary-only](https://example.org/summary-only)' in page


def test_registry_article_version_summary_is_retained_when_appearance_is_annotated():
    raw_summary = 'Raw report summary retained by the current article version.'
    annotated_summary = 'Annotated report summary from source metadata.'
    page = _render_registry_article({
        'article_id': 'article-annotated-summary',
        'canonical_url': 'https://example.org/annotated-summary',
        'title': 'Annotated summary article',
        'content': {}, 'available_content': {}, 'summary': None,
        'current_version_id': 'report-version-annotated', 'report_summary': raw_summary,
        'acquisition_observations': [], 'pdf_occurrences': [],
        'appearances': [{
            'version_id': 'report-version-annotated',
            'source_filename': 'climate-monitor-2026-09-28.md',
            'source_sha256': 'a' * 64,
            'original_url': 'https://example.org/annotated-original',
            'summary': annotated_summary,
        }],
    })

    assert page is not None
    assert raw_summary in page and annotated_summary in page
    assert '## Registry article-version summary' in page
    assert 'Registry article version: report-version-annotated' in page
    assert '## Report observation: climate-monitor-2026-09-28.md' in page
    assert 'Report citation: climate-monitor-2026-09-28.md; SHA-256: ' + 'a' * 64 in page


def test_registry_sync_generates_summary_only_article_without_appearance(tmp_path):
    database = tmp_path / 'registry.sqlite3'
    with sqlite3.connect(database) as connection:
        apply_migrations(connection)
        connection.execute("INSERT INTO sources VALUES ('example', 'example.org', 'Example', '2026-09-28', '2026-09-28')")
        connection.execute(
            "INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen, current_version_id, document_kind, publication_eligible, current_content_version_id, display_policy) VALUES ('article-summary-only', 'https://example.org/summary-only', 'example', '2026-09-28', '2026-09-28', 'report-version-only', 'article', 1, NULL, 'metadata_only')"
        )
        connection.execute(
            "INSERT INTO article_versions VALUES ('report-version-only', 'article-summary-only', 'Summary-only article', 'summary-only article', 'Unattached Registry report summary.', ?, 'report-title-summary', '2026-09-28', '2026-09-28')",
            (_sha('report-version-only'),),
        )
    sources, wiki = tmp_path / 'sources', tmp_path / 'wiki'
    sources.mkdir()
    wiki.mkdir()

    detail = RegistryReader(database, repository_root=tmp_path / 'app').article('article-summary-only')
    assert detail['current_version_id'] == 'report-version-only'
    sync_source_wiki(source_dir=sources, wiki_dir=wiki, cadence='weekly', registry_database=database)
    page = (wiki / 'article-article-summary-only.md').read_text(encoding='utf-8')
    assert '## Registry article-version summary' in page
    assert 'Unattached Registry report summary.' in page
    assert 'Registry article version: report-version-only' in page
    assert 'Article citation: [https://example.org/summary-only](https://example.org/summary-only)' in page


def test_registry_sync_generates_confirmed_and_source_only_pages(tmp_path):
    sources = tmp_path / 'sources'
    wiki = tmp_path / 'wiki'
    sources.mkdir()
    wiki.mkdir()
    source = sources / 'climate-monitor-2026-09-28.md'
    source.write_text('# Weekly report\n\n## Summary\n\nExisting weekly query.', encoding='utf-8')
    database = tmp_path / 'registry.sqlite3'
    _registry(database)

    first = sync_source_wiki(source_dir=sources, wiki_dir=wiki, cadence='weekly', registry_database=database)
    article = wiki / 'article-article-confirmed.md'
    observations = wiki / 'registry-source-observations.md'
    assert article.is_file() and observations.is_file()
    assert 'Confirmed article body about glacier pricing.' in article.read_text(encoding='utf-8')
    assert 'evidence.pdf' in article.read_text(encoding='utf-8')
    assert 'page 3' in article.read_text(encoding='utf-8') and 'page 4' in article.read_text(encoding='utf-8')
    assert 'Provenance: content_enrichment' in article.read_text(encoding='utf-8')
    assert 'Registry content version: content-v1' in article.read_text(encoding='utf-8')
    pdf_only = wiki / 'article-article-pdf-only.md'
    assert pdf_only.is_file() and 'PDF-only confirmed summary.' in pdf_only.read_text(encoding='utf-8')
    source_only = observations.read_text(encoding='utf-8')
    assert 'article details are unconfirmed' in source_only
    assert 'evidence.pdf' in source_only and 'page 7' in source_only
    assert not (wiki / 'article-pdf-unconfirmed.md').exists()

    source_before = source.read_bytes()
    second = sync_source_wiki(source_dir=sources, wiki_dir=wiki, cadence='weekly', registry_database=database)
    assert source.read_bytes() == source_before
    assert article.name in first.created_pages
    assert article.name in second.unchanged_pages

    with sqlite3.connect(database) as connection:
        content = '# Confirmed article\n\nUpdated glacier pricing body.'
        connection.execute(
            """INSERT INTO article_content_versions VALUES ('content-v2', 'article-confirmed', ?, ?, ?, 'text/html', 10,
               'html-to-markdown', '1', '2026-09-29T00:00:00Z')""",
            (_sha(content), content, _sha(content + 'markdown')),
        )
        connection.execute("UPDATE articles SET current_content_version_id='content-v2' WHERE article_id='article-confirmed'")
    updated = sync_source_wiki(source_dir=sources, wiki_dir=wiki, cadence='weekly', registry_database=database)
    assert article.name in updated.updated_pages
    assert 'Updated glacier pricing body.' in article.read_text(encoding='utf-8')
    assert 'Registry content version: content-v2' in article.read_text(encoding='utf-8')
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM article_content_versions WHERE article_id='article-confirmed'").fetchone()[0] == 2


def test_registry_pdf_occurrence_chunks_keep_distinct_headings_and_citations(tmp_path):
    sources, wiki = tmp_path / 'sources', tmp_path / 'wiki'
    sources.mkdir()
    wiki.mkdir()
    database = tmp_path / 'registry.sqlite3'
    _registry(database)
    sync_source_wiki(source_dir=sources, wiki_dir=wiki, cadence='weekly', registry_database=database)

    knowledge_base = WikiKnowledgeBase(wiki_dir=wiki, source_dir=sources)
    confirmed = knowledge_base.search('PDF observation confirmed article', top_k=20)
    unconfirmed = knowledge_base.search('unconfirmed PDF observatory signal', top_k=20)
    confirmed_chunks = [hit.chunk for hit in confirmed if hit.chunk.path == 'wiki/article-article-confirmed.md']
    unconfirmed_chunks = [hit.chunk for hit in unconfirmed if hit.chunk.path == 'wiki/registry-source-observations.md']

    assert {chunk.heading for chunk in confirmed_chunks} >= {
        'PDF report observation: occurrence-pdf-confirmed-3',
        'PDF report observation: occurrence-pdf-confirmed-4',
    }
    assert any('page 3' in chunk.text for chunk in confirmed_chunks)
    assert any('page 4' in chunk.text for chunk in confirmed_chunks)
    assert {chunk.heading for chunk in unconfirmed_chunks} >= {
        'PDF report observation: occurrence-pdf-unconfirmed-7',
        'PDF report observation: occurrence-pdf-unconfirmed-8',
    }
    assert any('page 7' in chunk.text for chunk in unconfirmed_chunks)
    assert any('page 8' in chunk.text for chunk in unconfirmed_chunks)



def test_registry_sync_reloads_the_same_app_and_chats_with_both_record_types(tmp_path, monkeypatch):
    sources = tmp_path / 'sources'
    wiki = tmp_path / 'wiki'
    sources.mkdir()
    wiki.mkdir()
    (sources / 'climate-monitor-2026-09-28.md').write_text(
        '# Weekly report\n\n## Summary\n\nExisting weekly query.', encoding='utf-8'
    )
    database = tmp_path / 'registry.sqlite3'
    _registry(database)
    sync_source_wiki(source_dir=sources, wiki_dir=wiki, cadence='weekly', registry_database=database)

    if sys.platform == 'win32':
        monkeypatch.setitem(sys.modules, 'fcntl', types.SimpleNamespace(
            LOCK_EX=0, LOCK_UN=0, LOCK_NB=0, flock=lambda *_args: None
        ))

    from fastapi.testclient import TestClient
    import api_server
    from agentic_wiki import AgenticWikiResponder

    responder = AgenticWikiResponder(wiki_dir=wiki, source_dir=sources)
    responder.client = None
    monkeypatch.setattr(api_server, 'responder', responder)
    monkeypatch.setattr(api_server, 'RELOAD_TOKEN', 'test-token')
    client = TestClient(api_server.app)
    assert client.post('/api/reload', headers={'x-reload-token': 'test-token'}).status_code == 200
    confirmed = client.post('/api/chat', json={'message': 'glacier pricing', 'answerMode': 'brief'}).json()
    unconfirmed = client.post('/api/chat', json={'message': 'observatory signal page 7', 'answerMode': 'brief'}).json()
    weekly = client.post('/api/chat', json={'message': 'Existing weekly query', 'answerMode': 'brief'}).json()
    assert any(item['path'] == 'wiki/article-article-confirmed.md' for item in confirmed['sources'])
    assert any(item['path'] == 'wiki/registry-source-observations.md' for item in unconfirmed['sources'])
    assert any(item['path'] == 'wiki/climate-monitor-2026-09-28.md' for item in weekly['sources'])


def test_registry_only_sync_reloads_and_chats_without_report_dates(tmp_path, monkeypatch):
    sources = tmp_path / 'sources'
    wiki = tmp_path / 'wiki'
    sources.mkdir()
    wiki.mkdir()
    database = tmp_path / 'registry.sqlite3'
    _registry(database)

    result = sync_source_wiki(source_dir=sources, wiki_dir=wiki, cadence='weekly', registry_database=database)
    assert result.latest_date == ''
    assert result.daily_pages == result.source_days == 0

    if sys.platform == 'win32':
        monkeypatch.setitem(sys.modules, 'fcntl', types.SimpleNamespace(
            LOCK_EX=0, LOCK_UN=0, LOCK_NB=0, flock=lambda *_args: None
        ))

    from fastapi.testclient import TestClient
    import api_server
    from agentic_wiki import AgenticWikiResponder

    responder = AgenticWikiResponder(wiki_dir=wiki, source_dir=sources)
    responder.client = None
    monkeypatch.setattr(api_server, 'responder', responder)
    monkeypatch.setattr(api_server, 'RELOAD_TOKEN', 'test-token')
    client = TestClient(api_server.app)
    assert client.post('/api/reload', headers={'x-reload-token': 'test-token'}).status_code == 200
    confirmed = client.post('/api/chat', json={'message': 'glacier pricing', 'answerMode': 'brief'}).json()
    unconfirmed = client.post('/api/chat', json={'message': 'observatory signal', 'answerMode': 'brief'}).json()
    assert any(item['path'] == 'wiki/article-article-confirmed.md' for item in confirmed['sources'])
    assert any(item['path'] == 'wiki/registry-source-observations.md' for item in unconfirmed['sources'])


def test_acquisition_projection_syncs_newest_body_and_all_origins_without_weekly_report(tmp_path, monkeypatch):
    database = tmp_path / 'registry.sqlite3'
    initialize_registry(database)
    url = 'https://example.org/acquired'
    first_body = '# Acquisition v1\n\nInitial glacier pricing evidence.'
    second_body = '# Acquisition v2\n\nNewer glacier pricing evidence.'
    search = {
        'search_ref': 'search-1', 'query': 'glacier pricing', 'engine': 'web_search', 'status': 'success',
        'attempted_at': '2026-09-28T08:00:00Z', 'result_refs': ['search-result-1'],
        'budget': {'max_results': 1, 'used_results': 1}, 'error': None,
    }
    store_acquisition_batch(database, _acquisition_batch([
        _acquisition_item(url, first_body, kind='site', ref='site:example'),
        _acquisition_item(url, first_body, kind='search', ref='search-result-1', search_ref='search-1'),
    ], batch_id='acquisition-v1', report_date='2026-09-28', searches=[search]))
    reader = RegistryReader(database, repository_root=tmp_path / 'app')
    article_id = reader.articles(page_size=10)['items'][0]['article_id']
    with sqlite3.connect(database) as connection:
        first_version_id = connection.execute(
            'SELECT content_version_id FROM article_content_versions WHERE article_id=?', (article_id,)
        ).fetchone()[0]
        connection.execute(
            'UPDATE articles SET current_content_version_id=? WHERE article_id=?',
            (first_version_id, article_id),
        )
        connection.execute(
            "INSERT INTO article_enrichments VALUES (?, ?, 'complete', ?, '[]', '[]', 'en', 'deterministic', 'test', '1', '2026-09-28T09:00:00Z', NULL, NULL)",
            ('summary-v1', first_version_id, 'Summary from body version one.'),
        )
    store_acquisition_batch(database, _acquisition_batch([
        _acquisition_item(url, second_body, kind='site', ref='site:example-v2', discovered_at='2026-09-29T08:00:00Z'),
    ], batch_id='acquisition-v2', report_date='2026-09-29', searches=[]))

    detail = reader.article(article_id)
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT current_content_version_id FROM articles WHERE article_id=?', (article_id,)).fetchone() == (first_version_id,)
    assert detail['content']['content_version_id'] == first_version_id
    assert detail['enrichment']['content_version_id'] == first_version_id
    assert detail['summary'] == 'Summary from body version one.'
    assert detail['available_content']['supporting_excerpt'] == ' '.join(second_body.split())
    assert detail['available_content']['content_version_id'] != first_version_id
    assert len(detail['acquisition_observations']) == 2
    assert {origin['discovery_kind'] for origin in detail['acquisition_observations'][0]['origins']} == {'search', 'site'}

    sources, wiki = tmp_path / 'sources', tmp_path / 'wiki'
    sources.mkdir()
    result = sync_source_wiki(source_dir=sources, wiki_dir=wiki, cadence='weekly', registry_database=database)
    page = (wiki / f'article-{article_id}.md').read_text(encoding='utf-8')
    assert result.latest_date == '' and 'Newer glacier pricing evidence.' in page and 'Initial glacier pricing evidence.' not in page
    assert f"Registry content version: {detail['available_content']['content_version_id']}" in page
    assert 'Summary from body version one.' in page
    assert f'Provenance: content_enrichment; content version: {first_version_id}' in page
    assert 'search-result-1' in page and 'search-' in page

    if sys.platform == 'win32':
        monkeypatch.setitem(sys.modules, 'fcntl', types.SimpleNamespace(
            LOCK_EX=0, LOCK_UN=0, LOCK_NB=0, flock=lambda *_args: None
        ))
    from fastapi.testclient import TestClient
    import api_server
    from agentic_wiki import AgenticWikiResponder

    responder = AgenticWikiResponder(wiki_dir=wiki, source_dir=sources)
    responder.client = None
    monkeypatch.setattr(api_server, 'responder', responder)
    monkeypatch.setattr(api_server, 'RELOAD_TOKEN', 'test-token')
    client = TestClient(api_server.app)
    assert client.post('/api/reload', headers={'x-reload-token': 'test-token'}).status_code == 200
    response = client.post('/api/chat', json={'message': 'newer glacier pricing evidence', 'answerMode': 'brief'}).json()
    assert any(item['path'] == f'wiki/article-{article_id}.md' for item in response['sources'])
