from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest

from climate_registry.acquisition import (
    AcquisitionIncompleteError,
    PublicationDatePolicy,
    freeze_acquisition_for_report,
    load_acquisition_batch,
    store_acquisition_batch,
)
from climate_registry.errors import RegistryInputError, RegistryLockError
from climate_registry.schema import apply_migrations
from climate_registry.persistent import _exclusive_database_lock, initialize_registry
from climate_monitor.article_content_adapter import validate_retained_article_evidence
from climate_monitor.models import CandidateItem
from climate_monitor.report_writer import render_report
from climate_monitor.taxonomy import load_article_taxonomy
from climate_monitor.weekly_monitor.authoring_contract import build_authoring_request
from climate_monitor.weekly_monitor.prompt_loader import load_weekly_monitor_prompt
from climate_monitor.weekly_monitor.driver import _candidate_items_from_evidence
from scripts import run_climate_monitor as monitor
from scripts.run_climate_monitor import (
    _store_restart_and_freeze_registry_acquisition,
    _validate_registry_acquisition_completeness,
)

NOW = "2026-09-10T08:00:00Z"


def _database(tmp_path):
    path = tmp_path / "registry.sqlite3"
    assert initialize_registry(path)["created"] is True
    return path


def _body(text="# Full article\n\nEvidence."):
    return text, hashlib.sha256(text.encode()).hexdigest()


def _item(url="https://example.org/article", *, body="# Full article\n\nEvidence.",
          published_date="2026-09-01", selected=True, status="ok",
          discovery_kind="search", discovery_ref="result-1",
          discovery_search_ref: str | None = "search-1",
          processing_status="complete", processing_error=None,
          source="Example Institute"):
    content, digest = _body(body)
    evidence = {
        "status": status, "fetched_at": NOW, "final_url": url,
        "attempts": [{"engine": "web_http", "status": "success" if status == "ok" else "failed",
                      "http_status": 200 if status == "ok" else 403,
                      "attempted_at": NOW}],
        "selected_method": "web_http", "content_type": "text/markdown",
        "content": content if status == "ok" else None,
        "content_hash": digest if status == "ok" else None,
        "content_ref": "managed/raw/article.md" if status == "ok" else None,
        "raw_snapshot_ref": "managed/raw/article.html" if status == "ok" else None,
        "raw_snapshot_sha256": "a" * 64 if status == "ok" else None,
        "classification": "full_content" if status == "ok" else "error",
        "failure_reason": None if status == "ok" else "blocked",
        "http_status": 200 if status == "ok" else 403,
    }
    return {
        "url": url, "title": "Article", "summary": "Search excerpt",
        "source": source, "discovered_at": NOW,
        "discovery_kind": discovery_kind, "discovery_ref": discovery_ref,
        "discovery_search_ref": discovery_search_ref,
        "published_date": published_date,
        "publication_date_evidence": ({"kind": "publisher", "url": url,
            "text": "Published 1 September 2026"} if published_date else None),
        "selected": selected, "selection_reason": "relevant" if selected else "not selected",
        "processing_status": processing_status, "processing_error": processing_error,
        "evidence": evidence,
    }


def _batch(items, *, batch_id="batch-112", policy=None, searches=None,
           search_decision=None, completed=True, report_date="2026-09-10"):
    return {
        "schema_version": "pre-report-acquisition-batch.v1", "batch_id": batch_id,
        "report_date": report_date,
        "started_at": NOW, "completed_at": NOW if completed else None,
        "date_policy": policy or PublicationDatePolicy.resolve(
            None, anchor_date=date(2026, 9, 10), frozen_at=NOW).to_dict(),
        "search_decision": search_decision or {"status": "attempted", "reason": None},
        "searches": searches if searches is not None else [{
            "search_ref": "search-1", "query": "agent chosen query", "engine": "web_search", "status": "success",
            "attempted_at": NOW, "result_refs": ["result-1"],
            "budget": {"max_results": 5, "used_results": 1}, "error": None,
        }],
        "items": items,
    }


def test_date_policy_default_recent_custom_boundaries_and_unknown():
    unlimited = PublicationDatePolicy.resolve(None, anchor_date=date(2026, 9, 10), frozen_at=NOW)
    assert unlimited.mode == "unlimited" and unlimited.search_time_filter is None
    assert unlimited.selects(None) is True and unlimited.selects(date(1999, 1, 1)) is True

    recent = PublicationDatePolicy.resolve({"mode": "recent", "days": 3},
        anchor_date=date(2026, 9, 10), frozen_at=NOW)
    assert recent.start == date(2026, 9, 8) and recent.end == date(2026, 9, 10)
    assert recent.selects(date(2026, 9, 8)) and recent.selects(date(2026, 9, 10))
    assert not recent.selects(date(2026, 9, 7)) and not recent.selects(None)

    custom = PublicationDatePolicy.resolve({"mode": "custom", "start": "2026-08-01", "end": "2026-08-31"},
        anchor_date=date(2026, 9, 10), frozen_at=NOW)
    assert custom.selects(date(2026, 8, 1)) and custom.selects(date(2026, 8, 31))
    assert not custom.selects(None)


def test_store_before_report_restart_dedupe_versions_unselected_and_exact_handoff(tmp_path):
    database = _database(tmp_path)
    first = store_acquisition_batch(database, _batch([
        _item(), _item("https://example.org/unselected", selected=False),
        _item("https://example.org/failed", status="failed", selected=False),
    ], completed=False))
    assert first["article_count"] == 3 and first["content_version_count"] == 2

    # A fresh read-only connection sees pre-report bodies and all history.
    loaded = load_acquisition_batch(database, "batch-112")
    assert len(loaded["items"]) == 3
    by_url = {row["canonical_url"]: row for row in loaded["items"]}
    assert {url: row["update_status"] for url, row in by_url.items()} == {
        "https://example.org/article": "baseline",
        "https://example.org/unselected": "baseline",
        "https://example.org/failed": "failed",
    }
    exact_id = by_url["https://example.org/article"]["content_version_id"]
    assert by_url["https://example.org/unselected"]["selection_status"] == "unselected"
    assert by_url["https://example.org/failed"]["material_status"] == "error"

    duplicate = store_acquisition_batch(database, _batch([_item()], batch_id="batch-duplicate"))
    assert duplicate["new_article_count"] == 0 and duplicate["new_content_version_count"] == 0

    changed = store_acquisition_batch(database, _batch([
        _item(body="# Full article\n\nChanged evidence.")], batch_id="batch-changed"))
    assert changed["new_content_version_count"] == 1
    assert load_acquisition_batch(database, "batch-changed")["items"][0][
        "update_status"
    ] == "content_changed"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM articles WHERE canonical_url = ?",
            ("https://example.org/article",)).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM article_content_versions WHERE article_id = ?",
            (loaded["items"][0]["article_id"],)).fetchone()[0] == 2

    handoff = freeze_acquisition_for_report(database, "batch-112", report_date="2026-09-10",
        allow_unresolved=True)
    assert handoff["schema_version"] == "article-evidence.v1"
    assert handoff["records"][0]["content_version_id"] == exact_id
    assert handoff["records"][0]["content"] == "# Full article\n\nEvidence."


def test_enabled_window_retains_unknown_for_reselection(tmp_path):
    database = _database(tmp_path)
    policy = PublicationDatePolicy.resolve({"mode": "recent", "days": 3},
        anchor_date=date(2026, 9, 10), frozen_at=NOW).to_dict()
    unknown = _item(published_date=None)
    old = _item("https://example.org/old", published_date="2026-01-01")
    result = store_acquisition_batch(database, _batch([unknown, old], policy=policy))
    assert result["selected_count"] == 0
    loaded = load_acquisition_batch(database, "batch-112")
    assert {row["date_status"] for row in loaded["items"]} == {"outside_window", "unknown_pending_review"}

    unlimited = PublicationDatePolicy.resolve(None, anchor_date=date(2026, 9, 10), frozen_at=NOW).to_dict()
    result = store_acquisition_batch(database, _batch([unknown, old], batch_id="batch-reselect", policy=unlimited))
    assert result["selected_count"] == 2


def test_search_success_failure_and_justified_no_search_are_distinct(tmp_path):
    database = _database(tmp_path)
    failed_search = [{"search_ref": "search-1", "query": "q", "engine": "web_search", "status": "failed",
        "attempted_at": NOW, "result_refs": [], "budget": {"max_results": 5, "used_results": 0},
        "error": "timeout"}]
    store_acquisition_batch(database, _batch([], searches=failed_search, completed=False))
    with pytest.raises(AcquisitionIncompleteError, match="unresolved"):
        freeze_acquisition_for_report(database, "batch-112", report_date="2026-09-10")

    no_search = _batch([], batch_id="batch-no-search", searches=[],
        search_decision={"status": "no_search", "reason": "site acquisition already covered all scoped institutions"})
    store_acquisition_batch(database, no_search)
    handoff = freeze_acquisition_for_report(database, "batch-no-search", report_date="2026-09-10")
    assert handoff["record_count"] == 0 and handoff["dependency_status"] == "available"

    bad = _batch([], batch_id="batch-bad", searches=[],
        search_decision={"status": "attempted", "reason": None})
    with pytest.raises(ValueError, match="attempted search requires"):
        store_acquisition_batch(database, bad)


def test_search_and_site_origins_have_explicit_valid_provenance(tmp_path):
    database = _database(tmp_path)
    with pytest.raises(ValueError, match="successful search attempt"):
        store_acquisition_batch(database, _batch([
            _item(discovery_ref="not-returned")]))

    failed = [{"search_ref": "search-1", "query": "q", "engine": "web_search",
               "status": "failed", "attempted_at": NOW, "result_refs": ["result-1"],
               "budget": {"max_results": 5, "used_results": 1}, "error": "timeout"}]
    with pytest.raises(ValueError, match="successful search attempt"):
        store_acquisition_batch(database, _batch([_item()], searches=failed))

    site = _item(discovery_kind="site", discovery_ref="site-scope:news",
                 discovery_search_ref=None)
    result = store_acquisition_batch(database, _batch([site], batch_id="site-batch"))
    assert result["article_count"] == 1

    with pytest.raises(ValueError, match="site discovery"):
        store_acquisition_batch(database, _batch([
            _item(discovery_kind="site", discovery_ref="site-scope:news")],
            batch_id="bad-site"))


@pytest.mark.parametrize("processing_status,processing_error", [
    ("pending", None), ("failed", "normalization failed"),
])
def test_processing_work_is_unselected_unresolved_and_cannot_mark_batch_complete(
    tmp_path, processing_status, processing_error
):
    database = _database(tmp_path)
    item = _item(processing_status=processing_status, processing_error=processing_error)
    with pytest.raises(ValueError, match="completed batch contains unresolved work"):
        store_acquisition_batch(database, _batch([item]))

    store_acquisition_batch(database, _batch([item], completed=False))
    loaded = load_acquisition_batch(database, "batch-112")
    assert loaded["items"][0]["selection_status"] == "unselected"
    with pytest.raises(AcquisitionIncompleteError, match="unresolved"):
        freeze_acquisition_for_report(database, "batch-112", report_date="2026-09-10")


def test_snippet_only_is_unresolved_even_when_zero_items_are_selected(tmp_path):
    database = _database(tmp_path)
    item = _item(status="failed", selected=False)
    item["evidence"].update(status="no_content", classification="snippet",
                            failure_reason="snippet only")
    store_acquisition_batch(database, _batch([item], completed=False))
    with pytest.raises(AcquisitionIncompleteError, match="unresolved"):
        freeze_acquisition_for_report(database, "batch-112", report_date="2026-09-10")
    diagnostic = freeze_acquisition_for_report(
        database, "batch-112", report_date="2026-09-10", allow_unresolved=True)
    assert diagnostic["dependency_status"] == "partial"
    assert diagnostic["record_count"] == 0


def test_same_batch_duplicate_url_and_body_merges_origins_deterministically(tmp_path):
    search = _item()
    site = _item(discovery_kind="site", discovery_ref="site-scope:publications",
                 discovery_search_ref=None)
    expected = None
    for name, items in (("forward", [search, site]), ("reverse", [site, search])):
        database = _database(tmp_path / name)
        summary = store_acquisition_batch(database, _batch(items))
        assert {key: summary[key] for key in (
            "batch_id", "article_count", "content_version_count", "selected_count"
        )} == {"batch_id": "batch-112", "article_count": 1,
               "content_version_count": 1, "selected_count": 1}
        loaded = load_acquisition_batch(database, "batch-112")
        assert len(loaded["items"]) == 1
        origins = loaded["items"][0]["origins"]
        assert [(origin["discovery_kind"], origin["discovery_ref"])
                for origin in origins] == [
                    ("search", "result-1"), ("site", "site-scope:publications")]
        frozen = freeze_acquisition_for_report(
            database, "batch-112", report_date="2026-09-10")
        current = (origins, frozen["records"][0]["origins"],
                   frozen["records"][0]["record_hash"])
        expected = expected or current
        assert current == expected


def test_same_batch_conflicting_selected_content_versions_are_rejected(tmp_path):
    database = _database(tmp_path)
    with pytest.raises(ValueError, match="conflicting selected content versions"):
        store_acquisition_batch(database, _batch([
            _item(body="# Version one"), _item(body="# Version two")]))


def test_acquisition_writer_uses_registry_replacement_lock_and_canonical_path(tmp_path):
    database = _database(tmp_path)
    with _exclusive_database_lock(database):
        with pytest.raises(RegistryLockError, match="locked"):
            store_acquisition_batch(database, _batch([_item()]))

    symlink = tmp_path / "registry-link.sqlite3"
    symlink.symlink_to(database)
    with pytest.raises(RegistryInputError, match="canonical regular file"):
        store_acquisition_batch(symlink, _batch([_item()]))


def test_batch_report_date_is_bound_to_policy_anchor_and_report_handoff(tmp_path):
    database = _database(tmp_path)
    with pytest.raises(ValueError, match="date policy anchor"):
        store_acquisition_batch(database, _batch([_item()], report_date="2026-09-09"))

    store_acquisition_batch(database, _batch([_item()]))
    assert load_acquisition_batch(database, "batch-112")["report_date"] == "2026-09-10"
    with pytest.raises(ValueError, match="report_date mismatch"):
        freeze_acquisition_for_report(database, "batch-112", report_date="2026-09-11")


def test_success_requires_distinct_content_and_raw_snapshot_provenance(tmp_path):
    database = _database(tmp_path)
    for missing in ("content_ref", "raw_snapshot_ref", "raw_snapshot_sha256"):
        item = _item()
        item["evidence"][missing] = None
        with pytest.raises(ValueError, match="successful full_content requires|must be paired"):
            store_acquisition_batch(database, _batch([item], batch_id=f"missing-{missing}"))

    item = _item()
    item["evidence"]["content_ref"] = item["evidence"]["raw_snapshot_ref"]
    with pytest.raises(ValueError, match="must be distinct"):
        store_acquisition_batch(database, _batch([item], batch_id="same-ref"))

    store_acquisition_batch(database, _batch([_item()]))
    loaded = load_acquisition_batch(database, "batch-112")["items"][0]
    assert loaded["content_ref"] == "managed/raw/article.md"
    assert loaded["raw_snapshot_ref"] == "managed/raw/article.html"
    frozen = freeze_acquisition_for_report(database, "batch-112", report_date="2026-09-10")
    assert frozen["records"][0]["content_ref"] == "managed/raw/article.md"
    assert frozen["records"][0]["extra"]["raw_snapshot_ref"] == "managed/raw/article.html"


@pytest.mark.parametrize("failed_engine", ["aaa_failed_engine", "zzz_failed_engine"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("success_kind", ["search", "site"])
def test_same_url_failure_is_resolved_by_success_without_losing_history(
    tmp_path, reverse, success_kind, failed_engine
):
    if success_kind == "search":
        failed = _item(status="failed", selected=False, discovery_kind="site",
                       discovery_ref="site-scope:news", discovery_search_ref=None)
        success = _item(discovery_kind="search", discovery_ref="result-1")
    else:
        failed = _item(status="failed", selected=False, discovery_kind="search",
                       discovery_ref="result-1", discovery_search_ref="search-1")
        success = _item(discovery_kind="site", discovery_ref="site-scope:news",
                        discovery_search_ref=None)
    failed["evidence"]["attempts"] = [
        {"engine": failed_engine, "status": "failed", "http_status": 403}
    ]
    items = [failed, success]
    if reverse:
        items.reverse()
    database = _database(tmp_path)

    summary = store_acquisition_batch(database, _batch(items))
    assert summary["article_count"] == summary["selected_count"] == 1
    loaded = load_acquisition_batch(database, "batch-112")
    assert len(loaded["items"]) == 2
    success_row = next(row for row in loaded["items"] if row["fetch_status"] == "success")
    failed_row = next(row for row in loaded["items"] if row["fetch_status"] == "failed")
    assert {attempt["status"] for attempt in success_row["attempts"]} == {
        "failed", "success"
    }
    assert {origin["disposition"] for origin in success_row["origins"]} == {
        "resolved_by_success", "successful"
    }
    assert failed_row["resolved_by_fetch_id"] == success_row["fetch_id"]
    assert failed_row["fetched_at"] == NOW
    assert failed_row["raw_url"] == "https://example.org/article"
    assert failed_row["final_url"] == "https://example.org/article"
    assert failed_row["http_status"] == 403
    assert failed_row["error_code"] == "failed"
    assert failed_row["error_message"] == "blocked"
    assert failed_row["material_status"] == "error"
    assert failed_row["content_ref"] is None
    assert failed_row["raw_snapshot_ref"] is None
    assert failed_row["raw_snapshot_sha256"] is None
    assert failed_row["attempts"] == [{
        "engine": failed_engine,
        "http_status": 403,
        "status": "failed",
    }]
    assert failed_row["processing_status"] == "complete"
    assert failed_row["processing_error"] is None
    assert failed_row["selection_status"] == "unselected"
    assert {origin["disposition"] for origin in failed_row["origins"]} == {
        "resolved_by_success"
    }

    # A fresh read connection and normal freeze both see the resolved batch.
    frozen = freeze_acquisition_for_report(
        database, "batch-112", report_date="2026-09-10")
    assert frozen["record_count"] == 1
    assert frozen["records"][0]["content"] == "# Full article\n\nEvidence."
    assert success_row["extraction_method"] == "web_http"
    assert frozen["records"][0]["selected_method"] == "web_http"


@pytest.mark.parametrize("processing_status", ["pending", "failed"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("same_body", [True, False])
def test_same_url_full_content_with_unresolved_processing_stays_unresolved(
    tmp_path, processing_status, reverse, same_body
):
    unresolved = _item(
        body="# Selected body" if same_body else "# Unresolved body",
        selected=False,
        discovery_kind="site",
        discovery_ref="site-scope:processing",
        discovery_search_ref=None,
        processing_status=processing_status,
        processing_error="normalization failed" if processing_status == "failed" else None,
    )
    complete = _item(body="# Selected body")
    items = [unresolved, complete]
    if reverse:
        items.reverse()
    database = _database(tmp_path)

    summary = store_acquisition_batch(database, _batch(items, completed=False))

    assert summary["article_count"] == 1
    assert summary["selected_count"] == 1
    loaded = load_acquisition_batch(database, "batch-112")
    assert len(loaded["items"]) == 2
    unresolved_row = next(
        row for row in loaded["items"] if row["processing_status"] == processing_status
    )
    complete_row = next(
        row for row in loaded["items"] if row["processing_status"] == "complete"
    )
    assert unresolved_row["fetch_status"] == "success"
    assert unresolved_row["resolved_by_fetch_id"] is None
    assert unresolved_row["selection_status"] == "unselected"
    assert complete_row["fetch_status"] == "success"
    assert complete_row["selection_status"] == "selected"
    with pytest.raises(AcquisitionIncompleteError, match="1 unresolved items"):
        freeze_acquisition_for_report(database, "batch-112", report_date="2026-09-10")


@pytest.mark.parametrize(
    "attempts",
    [
        [
            {"engine": "failed_engine", "status": "failed", "http_status": 403},
            {"engine": "web_http", "status": "success", "http_status": 200},
        ],
        [
            {"engine": "failed_engine", "status": "success", "http_status": 200,
             "content_hash": "0" * 64},
        ],
    ],
)
def test_successful_content_rejects_selected_method_without_matching_successful_attempt(
    tmp_path, attempts
):
    item = _item()
    item["evidence"]["selected_method"] = "failed_engine"
    item["evidence"]["attempts"] = attempts

    with pytest.raises(ValueError, match="selected_method must identify a successful attempt"):
        store_acquisition_batch(_database(tmp_path), _batch([item]))


def test_same_body_cannot_be_rebound_to_a_different_selected_method(tmp_path):
    database = _database(tmp_path)
    store_acquisition_batch(database, _batch([_item()], batch_id="first"))
    rebound = _item()
    rebound["evidence"]["selected_method"] = "browser_fetch"
    rebound["evidence"]["attempts"] = [
        {"engine": "browser_fetch", "status": "success", "http_status": 200}
    ]

    with pytest.raises(ValueError, match="content body already has a different extraction method"):
        store_acquisition_batch(database, _batch([rebound], batch_id="second"))


def test_same_batch_same_body_rejects_conflicting_selected_methods(tmp_path):
    first = _item()
    second = _item(discovery_kind="site", discovery_ref="site-scope:news",
                   discovery_search_ref=None, selected=False)
    second["evidence"]["selected_method"] = "browser_fetch"
    second["evidence"]["attempts"] = [
        {"engine": "browser_fetch", "status": "success", "http_status": 200}
    ]

    with pytest.raises(ValueError, match="same content body has conflicting selected methods"):
        store_acquisition_batch(_database(tmp_path), _batch([first, second]))


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_selected_requires_json_boolean(tmp_path, value):
    item = _item()
    item["selected"] = value
    with pytest.raises(ValueError, match="selected must be a boolean"):
        store_acquisition_batch(_database(tmp_path), _batch([item]))


@pytest.mark.parametrize("attempts, message", [
    ([], "attempts must record at least one attempt"),
    ([{}], "engine or tool"),
    ([{"engine": "http", "status": "mystery"}], "status is invalid"),
    ([{"engine": "http", "status": "failed", "attempted_at": "yesterday"}], "attempted_at"),
    ([{"engine": "http", "status": "failed", "http_status": True}], "http_status"),
    ([{"engine": "http", "status": "failed", "error": ""}], "error"),
])
def test_fetch_attempts_require_meaningful_provenance(tmp_path, attempts, message):
    item = _item(status="failed", selected=False)
    item["evidence"]["attempts"] = attempts
    with pytest.raises(ValueError, match=message):
        store_acquisition_batch(_database(tmp_path), _batch([item], completed=False))


def test_explicit_deferred_fetch_can_have_no_attempts(tmp_path):
    item = _item(status="deferred", selected=False)
    item["evidence"]["attempts"] = []
    store_acquisition_batch(_database(tmp_path), _batch([item], completed=False))


@pytest.mark.parametrize("field,value,message", [
    ("attempted_at", "not-a-timestamp", "attempted_at"),
    ("result_refs", ["", "https://result.example"], "result_refs"),
    ("result_refs", ["same", "same"], "result_refs"),
    ("budget", {}, "budget"),
    ("budget", {"max_results": True}, "budget"),
    ("budget", {"max_results": -1}, "budget"),
])
def test_search_attempt_provenance_fields_are_strict(tmp_path, field, value, message):
    candidate = _batch([_item()])
    candidate["searches"][0][field] = value
    with pytest.raises(ValueError, match=message):
        store_acquisition_batch(_database(tmp_path), candidate)


def test_selected_cannot_be_omitted(tmp_path):
    item = _item()
    item.pop("selected")
    with pytest.raises(ValueError, match="selected must be a boolean"):
        store_acquisition_batch(_database(tmp_path), _batch([item]))


def test_acquisition_writer_rejects_schema_v7_with_actionable_error(tmp_path):
    database = tmp_path / "registry-v7.sqlite"
    with sqlite3.connect(database) as connection:
        apply_migrations(connection, target_version=7)

    with pytest.raises(
        RegistryInputError,
        match="acquisition writes require registry schema 8; found schema 7; migrate the registry",
    ):
        store_acquisition_batch(database.resolve(), _batch([_item()]))


def test_registry_semantics_are_identity_bound_into_authoring_and_rendering(tmp_path):
    database = _database(tmp_path)
    old = _item(published_date="2019-01-01")
    store_acquisition_batch(database, _batch([old], batch_id="old"))
    changed = _item(body="# Full article\n\nChanged evidence.", published_date=None)
    store_acquisition_batch(database, _batch([changed], batch_id="changed"))
    frozen = freeze_acquisition_for_report(
        database, "changed", report_date="2026-09-10")
    record = frozen["records"][0]
    assert record["acquisition"] == {
        "discovered_at": NOW,
        "publication_date": None,
        "publication_date_evidence": None,
        "date_status": "eligible",
        "update_status": "content_changed",
    }
    shell = _candidate_items_from_evidence(None, frozen)[0]
    assert shell.acquisition_update_status == "content_changed"
    request = build_authoring_request(
        report_date=date(2026, 9, 10), items=[shell],
        prompt=load_weekly_monitor_prompt(), taxonomy=load_article_taxonomy(),
        article_evidence=frozen,
        stats={"total": 1, "updated": 1, "unchanged": 0,
               "blocked": 0, "failed": 0, "unresolved": 0},
    )
    assert request["articles"][0]["acquisition"] == record["acquisition"]

    rendered_item = replace(
        shell, published=record["acquisition"]["publication_date"] or "",
        detected_at=record["acquisition"]["discovered_at"],
        acquisition_date_status=record["acquisition"]["date_status"],
        acquisition_update_status=record["acquisition"]["update_status"],
    )
    report = render_report(report_date=date(2026, 9, 10), title="Weekly",
                           items=[rendered_item], dedup_notes=[], sites_monitored=1,
                           warnings=[])
    assert "**Published:** 2026-09-10T08:00:00Z" in report
    assert "**Acquisition update:** content_changed" in report
    assert "Publication date unknown; displaying discovery time" in report

    old_record = freeze_acquisition_for_report(
        database, "old", report_date="2026-09-10")["records"][0]
    assert old_record["acquisition"]["publication_date"] == "2019-01-01"
    assert old_record["acquisition"]["update_status"] == "baseline"
    old_frozen = freeze_acquisition_for_report(
        database, "old", report_date="2026-09-10")
    old_shell = _candidate_items_from_evidence(None, old_frozen)[0]
    old_request = build_authoring_request(
        report_date=date(2026, 9, 10), items=[old_shell],
        prompt=load_weekly_monitor_prompt(), taxonomy=load_article_taxonomy(),
        article_evidence=old_frozen,
        stats={"total": 1, "updated": 1, "unchanged": 0,
               "blocked": 0, "failed": 0, "unresolved": 0},
    )
    assert old_request["articles"][0]["acquisition"]["publication_date"] == "2019-01-01"

    store_acquisition_batch(database, _batch([changed], batch_id="unchanged"))
    unchanged = freeze_acquisition_for_report(
        database, "unchanged", report_date="2026-09-10")
    assert unchanged["record_count"] == 1
    assert unchanged["acquisition_dispositions"][0]["update_status"] == "unchanged"
    unchanged_record = unchanged["records"][0]
    unchanged_shell = _candidate_items_from_evidence(None, unchanged)[0]
    unchanged_request = build_authoring_request(
        report_date=date(2026, 9, 10), items=[unchanged_shell],
        prompt=load_weekly_monitor_prompt(), taxonomy=load_article_taxonomy(),
        article_evidence=unchanged,
        stats={"total": 1, "updated": 0, "unchanged": 1,
               "blocked": 0, "failed": 0, "unresolved": 0},
    )
    assert unchanged_request["acquisition_dispositions"][0]["update_status"] == "unchanged"
    unchanged_item = replace(
        unchanged_shell,
        detected_at=unchanged_record["acquisition"]["discovered_at"],
        acquisition_date_status=unchanged_record["acquisition"]["date_status"],
        acquisition_update_status=unchanged_record["acquisition"]["update_status"],
    )
    old_item = replace(
        old_shell, published=old_record["acquisition"]["publication_date"],
        detected_at=old_record["acquisition"]["discovered_at"],
        acquisition_date_status=old_record["acquisition"]["date_status"],
        acquisition_update_status=old_record["acquisition"]["update_status"],
    )
    downstream = render_report(
        report_date=date(2026, 9, 10), title="Weekly",
        items=[old_item, rendered_item, unchanged_item], dedup_notes=[],
        sites_monitored=1, warnings=[],
    )
    assert "**Published:** 2019-01-01" in downstream
    assert "**Acquisition update:** baseline" in downstream
    assert "**Acquisition update:** unchanged" in downstream
    assert "**Acquisition update:** content_changed" in downstream


def test_weekly_report_renders_registry_publication_and_update_semantics():
    base = CandidateItem(
        title="Article", url="https://example.org/article", summary="Summary",
        source_name="Example", lane="website", climate_related=True,
        actuarial_related=True, categories=("Climate Risk",), keywords=("risk",),
    )
    items = [
        replace(base, url="https://example.org/old", published="2019-01-01",
                acquisition_date_status="eligible", acquisition_update_status="baseline"),
        replace(base, url="https://example.org/unknown", published="",
                detected_at=NOW, acquisition_date_status="unknown_pending_review",
                acquisition_update_status="content_changed"),
        replace(base, url="https://example.org/unchanged", published="2026-09-01",
                acquisition_date_status="eligible", acquisition_update_status="unchanged"),
    ]
    report = render_report(
        report_date=date(2026, 9, 10), title="Weekly", items=items,
        dedup_notes=[], sites_monitored=1, warnings=[],
        weekly_stats={"total": 3, "updated": 2, "unchanged": 1,
                      "blocked": 0, "failed": 0, "unresolved": 0},
    )
    assert "**Published:** 2019-01-01" in report
    assert "**Acquisition update:** baseline" in report
    assert "**Published:** Unknown" in report
    assert "**Publication date:** unknown_pending_review" in report
    assert "Publication date unknown; discovery time is not a publication date." in report
    assert "**Acquisition update:** content_changed" in report
    assert "**Acquisition update:** unchanged" in report
    assert NOW not in report


def test_frozen_registry_evidence_passes_strict_retained_validation(tmp_path):
    database = _database(tmp_path)
    store_acquisition_batch(database, _batch([_item()]))

    frozen = freeze_acquisition_for_report(
        database, "batch-112", report_date="2026-09-10")

    validate_retained_article_evidence(
        frozen,
        report_date="2026-09-10",
        urls={"https://example.org/article"},
    )


def _completeness_contract():
    manifest = {
        "schema_version": "web-listening-manifest.v1",
        "manifest_id": "manifest-a",
        "source": {"source_id": "site-a"},
        "run": {"run_id": "run-1"},
        "discovered_items": [
            {"item_id": "site-result-1", "url": "https://example.org/shared"},
            {"item_id": "site-result-2", "url": "https://example.org/shared"},
        ],
    }
    searches = [
        {"search_ref": "search-ok", "query": "chosen query", "engine": "web_search",
         "status": "success", "attempted_at": NOW, "result_refs": ["search-result-1"],
         "budget": {"max_results": 5, "used_results": 1}, "error": None},
        {"search_ref": "search-failed", "query": "follow-up", "engine": "web_search",
         "status": "failed", "attempted_at": NOW, "result_refs": [],
         "budget": {"max_results": 5, "used_results": 0}, "error": "timeout"},
    ]
    pillar_b = {
        "schema_version": "pillar-b-discovery.v2", "report_date": "2026-09-10",
        "date_policy": PublicationDatePolicy.resolve(
            None, anchor_date=date(2026, 9, 10), frozen_at=NOW).to_dict(),
        "search_decision": {"status": "attempted", "reason": None},
        "searches": searches,
        "articles": [{
            "title": "Search article", "url": "https://example.org/search",
            "source": "Search Source", "summary": "Result excerpt",
            "published_date": "2026-09-01",
            "date_evidence": {"kind": "publisher", "url": "https://example.org/search",
                              "text": "Published 1 September 2026"},
            "search_ref": "search-ok", "result_ref": "search-result-1",
        }],
    }
    items = [
        _item("https://example.org/shared", discovery_kind="site",
              discovery_ref="site-result-1", discovery_search_ref=None,
              source="site-a", selected=False),
        _item("https://example.org/shared", discovery_kind="site",
              discovery_ref="site-result-2", discovery_search_ref=None,
              source="site-a", status="failed", selected=False),
        _item("https://example.org/search", discovery_kind="search",
              discovery_ref="search-result-1", discovery_search_ref="search-ok",
              source="Search Source"),
    ]
    batch = _batch(items, searches=deepcopy(searches), completed=False)
    return manifest, pillar_b, batch


def test_registry_acquisition_completeness_accepts_every_exact_occurrence():
    manifest, pillar_b, batch = _completeness_contract()
    identity = _validate_registry_acquisition_completeness(batch, manifest, pillar_b)
    assert identity["site_occurrence_count"] == 2
    assert identity["search_occurrence_count"] == 1
    assert identity["fetch_processing_observation_count"] == 3
    assert len(identity["manifest_sha256"]) == 64


@pytest.mark.parametrize("missing", ["unselected", "failed_fetch", "failed_search", "duplicate_origin"])
def test_registry_acquisition_completeness_rejects_every_omission(missing):
    manifest, pillar_b, batch = _completeness_contract()
    if missing == "unselected":
        batch["items"].pop(0)
    elif missing == "failed_fetch":
        batch["items"].pop(1)
    elif missing == "failed_search":
        batch["searches"].pop(1)
    else:
        manifest["discovered_items"].append(
            {"item_id": "site-result-3", "url": "https://example.org/shared"}
        )
    with pytest.raises(ValueError, match="complete upstream acquisition manifest"):
        _validate_registry_acquisition_completeness(batch, manifest, pillar_b)


def test_registry_acquisition_completeness_rejects_substituted_occurrence():
    manifest, pillar_b, batch = _completeness_contract()
    batch["items"][1]["discovery_ref"] = "substituted"
    with pytest.raises(ValueError, match="complete upstream acquisition manifest"):
        _validate_registry_acquisition_completeness(batch, manifest, pillar_b)


def test_actual_prepare_registry_branch_accepts_frozen_evidence(tmp_path, monkeypatch):
    from pillar_b_fixture import discovery_fixture

    fixture = monitor.ROOT / "tests/fixtures/issue87/wri_repro"
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name in ("acquisition-batch-result.v2.json", "manifest.json"):
        (inputs / name).write_bytes((fixture / name).read_bytes())
    pillar_payload = discovery_fixture()
    (inputs / "pillar_b.discovery.json").write_text(
        json.dumps(pillar_payload), encoding="utf-8"
    )
    outcome = inputs / "acquisition-batch-result.v2.json"
    manifest = inputs / "manifest.json"
    pillar_b = inputs / "pillar_b.discovery.json"
    monkeypatch.setenv("CLIMATE_DRY_RUN", "1")
    monkeypatch.setenv("CLIMATE_DRY_RUN_OUTCOME_FIXTURE", "1")
    monkeypatch.setenv("CLIMATE_DRY_RUN_ROOT", str(tmp_path))

    def prepare(staging, *registry_args):
        monkeypatch.setattr(sys, "argv", [
            "run_climate_monitor.py", "--production-weekly",
            "--authoring-mode", "prepare", "--report-date", "2026-09-07",
            "--acquisition-batch", str(outcome),
            "--web-listening-manifest", str(manifest),
            "--pillar-b-artifact", str(pillar_b),
            "--staging-dir", str(staging),
            "--state-dir", str(tmp_path / "state"),
            "--source-dir", str(tmp_path / "sources"),
            "--wiki-dir", str(tmp_path / "wiki"),
            "--no-sync", "--no-update-seen-state", "--no-page-titles",
            "--article-evidence-loopback",
            "scripts.hermes_job:dry_run_unavailable_provider",
            *registry_args,
        ])
        monitor.main()

    discovered = json.loads(manifest.read_text(encoding="utf-8"))["discovered_items"]
    urls = {monitor.canonical_url(item["url"]) for item in discovered}
    assert len(urls) == 154

    items = [
        _item(
            item["url"],
            body=f"# Registry article\n\n{monitor.canonical_url(item['url'])}",
            discovery_kind="site",
            discovery_ref=item["item_id"],
            discovery_search_ref=None,
            source="wri",
        )
        for item in discovered
    ]
    payload = _batch(
        items,
        batch_id="prepare-integration",
        searches=deepcopy(pillar_payload["searches"]),
        search_decision=deepcopy(pillar_payload["search_decision"]),
        report_date="2026-09-07",
        policy=deepcopy(pillar_payload["date_policy"]),
    )
    registry_input = tmp_path / "registry-input.json"
    registry_input.write_text(json.dumps(payload), encoding="utf-8")
    database = _database(tmp_path)

    registry_staging = tmp_path / "registry-staging"
    prepare(
        registry_staging,
        "--registry-database", str(database),
        "--registry-acquisition-batch-id", "prepare-integration",
        "--registry-acquisition-input", str(registry_input),
    )

    bundle = monitor._read_staging_bundle(registry_staging)
    monitor._verify_staging_digest(registry_staging, bundle)
    evidence = json.loads(
        (registry_staging / "article_evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["record_count"] == len(urls)
    assert len(evidence["acquisition_dispositions"]) == len(urls)
    assert bundle["registry_acquisition"]["batch_id"] == "prepare-integration"
    completeness = bundle["registry_acquisition"]["ingestion"]["completeness"]
    assert completeness["site_occurrence_count"] == len(discovered)
    assert completeness["fetch_processing_observation_count"] == len(discovered)

    failed_pillar = deepcopy(pillar_payload)
    failed_pillar["searches"][0].update(
        status="failed", result_refs=[], error="native search timeout"
    )
    failed_pillar["articles"] = []
    pillar_b.write_text(json.dumps(failed_pillar), encoding="utf-8")
    failed_payload = deepcopy(payload)
    failed_payload.update(batch_id="prepare-failed-search", completed_at=None)
    failed_payload["searches"] = deepcopy(failed_pillar["searches"])
    failed_input = tmp_path / "registry-failed-search.json"
    failed_input.write_text(json.dumps(failed_payload), encoding="utf-8")
    failed_staging = tmp_path / "registry-failed-staging"

    with pytest.raises(SystemExit, match="unresolved work: 1 failed searches"):
        prepare(
            failed_staging,
            "--registry-database", str(database),
            "--registry-acquisition-batch-id", "prepare-failed-search",
            "--registry-acquisition-input", str(failed_input),
        )
    restarted = load_acquisition_batch(database, "prepare-failed-search")
    assert restarted["searches"][0]["status"] == "failed"
    assert restarted["searches"][0]["error_message"] == "native search timeout"
    assert not (failed_staging / "v2_authoring_request.json").exists()


def test_production_store_restart_freeze_helper_uses_exact_durable_batch(tmp_path):
    database = _database(tmp_path)
    payload = _batch([_item()])
    input_path = tmp_path / "pre-report.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    frozen, identity = _store_restart_and_freeze_registry_acquisition(
        database=str(database), input_path=str(input_path), batch_id="batch-112",
        report_date="2026-09-10")

    assert frozen["record_count"] == 1
    assert identity["store_summary"]["selected_count"] == 1
    assert identity["input_sha256"] == hashlib.sha256(input_path.read_bytes()).hexdigest()
    restarted = load_acquisition_batch(database, "batch-112")
    assert restarted["items"][0]["content_version_id"] == frozen["records"][0]["content_version_id"]


def test_normal_production_prepare_fails_closed_without_registry_ingestion_arguments():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("CLIMATE_DRY_RUN")}
    completed = subprocess.run([
        sys.executable, "scripts/run_climate_monitor.py", "--production-weekly",
        "--authoring-mode", "prepare", "--acquisition-batch", "/tmp/outcome.json",
        "--web-listening-manifest", "/tmp/manifest.json", "--pillar-b-artifact",
        "/tmp/pillar-b.json", "--staging-dir", "/tmp/staging", "--report-date",
        "2026-09-10",
    ], cwd=os.fspath(__import__("pathlib").Path(__file__).resolve().parents[1]),
       env=env, text=True, capture_output=True)
    assert completed.returncode == 2
    assert "production prepare/run requires the Registry store-before-freeze" in completed.stderr
