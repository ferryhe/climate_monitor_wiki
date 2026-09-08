from copy import deepcopy
from datetime import date
import hashlib
import json

import pytest

from climate_monitor.article_candidate_contract import adapt_pillar_b
from climate_monitor.weekly_monitor.pillar_b_discovery import validate_discovery
from climate_monitor.weekly_monitor.prompt_loader import pillar_b_search_queries
from scripts.run_climate_monitor import _read_pillar_b

DAY = date(2026, 9, 7)


def payload():
    return {
        "schema_version": "pillar-b-discovery.v1", "report_date": DAY.isoformat(),
        "searches": [{"query": query, "status": "completed"} for query in pillar_b_search_queries(DAY)],
        "articles": [{"title": "Climate insurance research", "url": "https://example.org/article",
                      "source": "Research Institute", "summary": "Research on climate insurance risk.",
                      "published_date": "2026-06-07",
                      "date_evidence": {"url": "https://example.org/article", "text": "Published 7 June 2026"}}],
    }


def test_dated_envelope_preserves_original_artifact_pointer_and_bytes(tmp_path):
    original = payload()
    path = tmp_path / "discovery.json"
    raw = json.dumps(original).encode()
    path.write_bytes(raw)
    accepted = _read_pillar_b(path, report_date=DAY.isoformat())
    candidates = adapt_pillar_b(accepted, artifact_id=path.name,
        artifact_sha256=hashlib.sha256(raw).hexdigest(), discovered_at="2026-09-07T08:00:00Z")
    assert candidates[0].origins[0].row == "/articles/0"
    assert candidates[0].origins[0].source == "Research Institute"
    assert accepted == original
    assert path.read_bytes() == raw


@pytest.mark.parametrize("published", ["2026-01-01", "2026-06-06", "2026-09-08", "", None, "2026-09", "20260907"])
def test_old_future_missing_and_nonexact_dates_are_rejected(published):
    value = payload()
    value["articles"][0]["published_date"] = published
    with pytest.raises(ValueError, match="publication date"):
        validate_discovery(value, report_date=DAY)


@pytest.mark.parametrize("mutation", ["missing", "failed", "duplicate", "stale", "foreign_evidence", "no_evidence"])
def test_incomplete_search_or_unbound_date_evidence_is_rejected(mutation):
    value = payload()
    if mutation == "missing": value["searches"].pop()
    elif mutation == "failed": value["searches"][0]["status"] = "failed"
    elif mutation == "duplicate": value["searches"][1] = deepcopy(value["searches"][0])
    elif mutation == "stale": value["report_date"] = "2026-08-31"
    elif mutation == "foreign_evidence": value["articles"][0]["date_evidence"]["url"] = "https://example.org/other"
    else: value["articles"][0]["date_evidence"]["text"] = " "
    with pytest.raises(ValueError): validate_discovery(value, report_date=DAY)


def test_zero_results_require_completed_queries_and_legacy_array_is_not_production(tmp_path):
    value = payload()
    value["articles"] = []
    assert validate_discovery(value, report_date=DAY) == []
    value["searches"] = []
    with pytest.raises(ValueError, match="query set"): validate_discovery(value, report_date=DAY)
    path = tmp_path / "legacy.json"
    path.write_text("[]")
    assert _read_pillar_b(path) == []
    with pytest.raises(SystemExit, match="requires pillar-b-discovery.v1"):
        _read_pillar_b(path, report_date=DAY.isoformat())


def test_no_article_count_cap():
    value = payload()
    value["articles"] = [deepcopy(value["articles"][0]) for _ in range(100)]
    assert len(validate_discovery(value, report_date=DAY)) == 100
