import hashlib
import json
import sqlite3
import sys
import types
import subprocess
from pathlib import Path

import pytest
from datetime import date

from climate_registry.schema import apply_migrations
from climate_registry.acquisition import PublicationDatePolicy, store_acquisition_batch
from climate_registry.persistent import initialize_registry
from climate_registry.read_api import RegistryReader
from climate_registry.wiki import sync_registry_wiki
from agentic_wiki.wiki_agent import WikiKnowledgeBase
from scripts.sync_source_wiki import _render_registry_article, sync_source_wiki


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _registry(path):
    with sqlite3.connect(path) as connection:
        apply_migrations(connection,target_version=20)
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
            """INSERT INTO article_enrichments(
                   enrichment_id, content_version_id, status, summary, categories_json,
                   keywords_json, language, generator_kind, generator_name,
                   generator_version, generated_at, error_code, error_message
               ) VALUES ('enrichment-v1', 'content-v1', 'complete', 'Confirmed semantic summary.',
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
        apply_migrations(connection,target_version=20)
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


def test_registry_wiki_sync_keeps_real_report_filename_and_hash(tmp_path):
    database = tmp_path / 'registry.sqlite3'
    _registry(database)
    report_sha = 'e' * 64
    report_filename = 'climate-monitor-2026-09-28.md'
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO reports VALUES (?, ?, ?, ?, ?, 'weekly', 'weekly-pillars-v1', 1, 1, 0, '[]')",
            ('report-2026-09-28', '2026-09-28', report_filename,
             'Weekly climate report', report_sha),
        )
        connection.execute(
            """INSERT INTO discoveries VALUES
               ('discovery-report-article', 'report-2026-09-28', 1, 'Pillar A', 'A',
                'article-confirmed', 'article-version', 'https://example.org/confirmed',
                'Confirmed climate article', 'Report summary for confirmed article.', 1, NULL)"""
        )
        connection.execute(
            """INSERT INTO report_appearances(
                   report_id, article_id, version_id, discovery_id, section, pillar,
                   ordinal, disposition
               ) VALUES ('report-2026-09-28', 'article-confirmed', 'article-version',
                   'discovery-report-article', 'Pillar A', 'A', 1, 'new')"""
        )

    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    states = sync_registry_wiki(database, wiki)
    page = (wiki / 'article-article-confirmed.md').read_text(encoding='utf-8')

    assert states['article-article-confirmed.md'] == 'created'
    assert f'## Report observation: {report_filename}' in page
    assert f'Report citation: {report_filename}; SHA-256: {report_sha}' in page


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


def test_registry_sync_keeps_manual_enrichment_provenance(tmp_path):
    database = tmp_path / "registry.sqlite3"
    wiki = tmp_path / "wiki"
    _registry(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO article_enrichments(
                   enrichment_id, content_version_id, status, summary, categories_json,
                   keywords_json, language, generator_kind, generator_name,
                   generator_version, generated_at
               ) VALUES ('manual-v1', 'content-v1', 'complete', 'Human-checked summary.',
                         '[\"Climate\"]', '[\"glacier pricing\"]', 'en', 'manual',
                         'human-review', 'manual-v1', '2026-09-29T00:00:00Z')"""
        )

    sync_registry_wiki(database, wiki)
    page = (wiki / "article-article-confirmed.md").read_text(encoding="utf-8")
    assert "Provenance: manual_enrichment" in page


def test_registry_sync_removes_only_generated_articles_absent_from_projection(tmp_path):
    sources, wiki = tmp_path / "sources", tmp_path / "wiki"
    sources.mkdir()
    wiki.mkdir()
    database = tmp_path / "registry.sqlite3"
    _registry(database)
    sync_source_wiki(
        source_dir=sources, wiki_dir=wiki, cadence="weekly",
        registry_database=database,
    )
    retired = wiki / "article-article-confirmed.md"
    retained = wiki / "article-article-pdf-only.md"
    manual = wiki / "article-manual.notes.md"
    manual.write_text("manual page", encoding="utf-8")
    assert retired.is_file() and retained.is_file()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE articles SET publication_eligible=0, document_kind='landing_page' "
            "WHERE article_id='article-confirmed'"
        )
    result = sync_source_wiki(
        source_dir=sources, wiki_dir=wiki, cadence="weekly",
        registry_database=database,
    )

    assert not retired.exists()
    assert retained.is_file()
    assert manual.read_text(encoding="utf-8") == "manual page"
    assert retired.name in result.pruned_pages


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


def test_legacy_acquisition_projection_syncs_newest_body_and_all_origins_without_weekly_report(tmp_path, monkeypatch):
    database = tmp_path / 'current-fixture.sqlite3'
    # Build facts through the supported current writer, without approving them.
    with sqlite3.connect(database) as connection:
        apply_migrations(connection,target_version=22)
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
    assert RegistryReader(database, repository_root=tmp_path / 'app').articles()['items'] == []
    with sqlite3.connect(database) as connection:
        article_id = connection.execute('SELECT article_id FROM articles WHERE canonical_url=?', (url,)).fetchone()[0]
        first_version_id = connection.execute(
            'SELECT content_version_id FROM article_content_versions WHERE article_id=?', (article_id,)
        ).fetchone()[0]
        connection.execute(
            'UPDATE articles SET current_content_version_id=? WHERE article_id=?',
            (first_version_id, article_id),
        )
        connection.execute(
            "INSERT INTO article_enrichments(enrichment_id,content_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at,error_code,error_message) VALUES (?, ?, 'complete', ?, '[]', '[]', 'en', 'deterministic', 'test', '1', '2026-09-28T09:00:00Z', NULL, NULL)",
            ('summary-v1', first_version_id, 'Summary from body version one.'),
        )
    store_acquisition_batch(database, _acquisition_batch([
        _acquisition_item(url, second_body, kind='site', ref='site:example-v2', discovered_at='2026-09-29T08:00:00Z'),
    ], batch_id='acquisition-v2', report_date='2026-09-29', searches=[]))

    # This case tests the retained historical readonly projection, not a schema20 writer.
    # Seed its existing SQL facts from the current writer's compatible business rows.
    historical = tmp_path / 'historical-schema20.sqlite3'
    with sqlite3.connect(historical) as connection:
        apply_migrations(connection, target_version=20)
        connection.execute('ATTACH DATABASE ? AS fixture', (str(database),))
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'")]
        for table in tables:
            columns = [row[1] for row in connection.execute('PRAGMA table_info(' + table + ')')]
            names = ','.join(columns)
            selected = ','.join('NULL' if table == 'articles' and name in {'current_version_id', 'current_content_version_id'} else name for name in columns)
            connection.execute('INSERT INTO ' + table + '(' + names + ') SELECT ' + selected + ' FROM fixture.' + table)
        connection.execute('UPDATE articles SET current_version_id=(SELECT current_version_id FROM fixture.articles WHERE fixture.articles.article_id=articles.article_id), current_content_version_id=(SELECT current_content_version_id FROM fixture.articles WHERE fixture.articles.article_id=articles.article_id)')
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 20
    assert RegistryReader(database, repository_root=tmp_path / 'app').articles()['items'] == []
    database = historical
    before = database.read_bytes()
    reader = RegistryReader(database, repository_root=tmp_path / 'app')
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

    assert database.read_bytes() == before  # Historical reading/rendering/reload never writes this DB.



def _env_public_registry(tmp_path):
    from climate_registry.audit import build_audit_registry
    from climate_registry.persistent import update_registry
    sources=tmp_path/'sources';sources.mkdir()
    def report(day,title,url):
        path=sources/f'climate-monitor-{day}.md'
        path.write_text(f'# Weekly Climate & Actuarial Monitor\n**Report Date:** {day}\n'
            '## Executive Summary\n- Sites checked: **1**, succeeded: **1**, failed: **0**\n'
            '- RAW EXECUTIVE MUST NOT REGENERATE\n## Pillar A — Changes\n'
            f'- **{title}** (web)\n  - {title} summary.\n  🔗 {url}\n'
            f'## Pillar B — Intelligence\n## Original Links\n- {url}\n',encoding='utf-8')
    report('2026-09-07','Approved existing article','https://example.org/approved')
    database=tmp_path/'registry.sqlite3'
    build_audit_registry(sources,database,tmp_path/'audit')
    report('2026-09-14','Pending new article','https://example.org/pending')
    update_registry(sources,database,tmp_path/'backups')
    with sqlite3.connect(database) as db:
        identities=dict(db.execute('SELECT canonical_url,article_id FROM articles'))
    return sources,database,identities


def test_env_only_sync_library_and_actual_cli_preserve_approved_hidden_pending_and_restore(tmp_path,monkeypatch):
    from climate_registry.publication import set_visibility
    sources,database,identities=_env_public_registry(tmp_path)
    monkeypatch.setenv('CLIMATE_REGISTRY_DB',str(database))
    source_bytes={p.name:p.read_bytes() for p in sources.iterdir()}
    identity=identities['https://example.org/approved'];pending=identities['https://example.org/pending']
    wiki=tmp_path/'wiki';cli_wiki=tmp_path/'cli-wiki'
    script=Path(__file__).resolve().parents[1]/'scripts/sync_source_wiki.py'
    for visible in (True,False,True):
        set_visibility(database,'article',identity,visible)
        before=database.read_bytes();inode=database.stat().st_ino
        sync_source_wiki(source_dir=sources,wiki_dir=wiki,cadence='weekly')
        run=subprocess.run([sys.executable,str(script),'--source-dir',str(sources),'--wiki-dir',str(cli_wiki),'--cadence','weekly'],capture_output=True,text=True)
        assert run.returncode==0,run.stderr
        for folder in (wiki,cli_wiki):
            payload=json.loads((folder/'public-registry.json').read_text())
            assert {item['article_id'] for item in payload['articles']}==({identity} if visible else set())
            assert (folder/f'article-{identity}.md').exists()==visible
            assert not (folder/f'article-{pending}.md').exists()
            assert not (folder/'climate-monitor-2026-09-14.md').exists()
            assert all('RAW EXECUTIVE MUST NOT REGENERATE' not in p.read_text() for p in folder.glob('*.md'))
        assert database.read_bytes()==before and database.stat().st_ino==inode
    assert source_bytes=={p.name:p.read_bytes() for p in sources.iterdir()}


@pytest.mark.parametrize('invalid',['missing','corrupt','unsupported'])
def test_invalid_env_sync_fails_before_library_or_cli_wiki_changes(tmp_path,monkeypatch,invalid):
    from climate_registry.read_api import RegistryError
    sources,database,_identities=_env_public_registry(tmp_path)
    bad=tmp_path/'invalid.sqlite3'
    if invalid=='corrupt':bad.write_bytes(b'not a SQLite database')
    elif invalid=='unsupported':
        with sqlite3.connect(bad) as db:db.execute('PRAGMA user_version=99')
    monkeypatch.setenv('CLIMATE_REGISTRY_DB',str(bad))
    wiki=tmp_path/'wiki';wiki.mkdir();(wiki/'index.md').write_text('Previous approved output\n')
    before={p.name:p.read_bytes() for p in wiki.iterdir()};source_bytes={p.name:p.read_bytes() for p in sources.iterdir()}
    with pytest.raises((ValueError,RegistryError)):
        sync_source_wiki(source_dir=sources,wiki_dir=wiki,cadence='weekly')
    script=Path(__file__).resolve().parents[1]/'scripts/sync_source_wiki.py'
    run=subprocess.run([sys.executable,str(script),'--source-dir',str(sources),'--wiki-dir',str(wiki),'--cadence','weekly'],capture_output=True,text=True)
    assert run.returncode!=0
    assert before=={p.name:p.read_bytes() for p in wiki.iterdir()}
    assert source_bytes=={p.name:p.read_bytes() for p in sources.iterdir()}


def test_explicit_relative_copy_sync_overrides_invalid_env_and_empty_config_keeps_static(tmp_path,monkeypatch):
    sources,database,identities=_env_public_registry(tmp_path)
    monkeypatch.chdir(tmp_path);monkeypatch.setenv('CLIMATE_REGISTRY_DB',str(tmp_path/'missing.sqlite3'))
    sync_source_wiki(source_dir=sources,wiki_dir=tmp_path/'explicit',cadence='weekly',registry_database=Path(database.name))
    payload=json.loads((tmp_path/'explicit/public-registry.json').read_text())
    assert {item['article_id'] for item in payload['articles']}=={identities['https://example.org/approved']}
    for index,value in enumerate((None,'','  ')):
        if value is None:monkeypatch.delenv('CLIMATE_REGISTRY_DB',raising=False)
        else:monkeypatch.setenv('CLIMATE_REGISTRY_DB',value)
        wiki=tmp_path/f'static-{index}'
        sync_source_wiki(source_dir=sources,wiki_dir=wiki,cadence='weekly')
        assert 'RAW EXECUTIVE MUST NOT REGENERATE' in (wiki/'climate-monitor-2026-09-07.md').read_text()
        assert not (wiki/'public-registry.json').exists()
