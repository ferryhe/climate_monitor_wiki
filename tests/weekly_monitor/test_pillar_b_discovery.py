from copy import deepcopy
from datetime import date
import hashlib
import json

import pytest

from climate_monitor.article_candidate_contract import adapt_pillar_b
from climate_monitor.weekly_monitor.pillar_b_discovery import (
    PillarBIncompleteError,
    validate_discovery,
)
from climate_monitor.weekly_monitor.prompt_loader import load_pillar_b_search_prompt
from climate_registry.acquisition import PublicationDatePolicy
from scripts.run_climate_monitor import _read_pillar_b

DAY = date(2026, 9, 7)
NOW = "2026-09-07T08:00:00Z"


def policy(config=None):
    return PublicationDatePolicy.resolve(config, anchor_date=DAY, frozen_at=NOW).to_dict()


def payload(*, date_policy=None):
    return {
        "schema_version": "pillar-b-discovery.v2", "report_date": DAY.isoformat(),
        "date_policy": date_policy or policy(),
        "search_decision": {"status": "attempted", "reason": None},
        "searches": [{"search_ref": "search-1", "query": "agent chose this query", "engine": "web_search",
                      "status": "success", "attempted_at": NOW,
                      "result_refs": ["https://search.example/result/1"],
                      "budget": {"max_results": 5, "used_results": 1}, "error": None}],
        "articles": [{"title": "Climate insurance research", "url": "https://example.org/article",
                      "source": "Research Institute", "summary": "Research on climate insurance risk.",
                      "published_date": "2026-06-07",
                      "date_evidence": {"kind": "publisher", "url": "https://example.org/article",
                                        "text": "Published 7 June 2026"},
                      "search_ref": "search-1",
                      "result_ref": "https://search.example/result/1"}],
    }


def test_prompt_defaults_unlimited_and_has_no_fixed_query_or_time_filter(tmp_path):
    rendered = load_pillar_b_search_prompt(DAY, (tmp_path / "out.json").resolve(), frozen_at=NOW)
    text = rendered.raw_bytes.decode()
    assert rendered.version == "v2"
    assert "do not add search time parameters" in text
    assert "no fixed query list" in text
    assert "after:" not in text and "before:" not in text


def test_custom_prompt_freezes_inclusive_search_guidance(tmp_path):
    rendered = load_pillar_b_search_prompt(
        DAY, (tmp_path / "out.json").resolve(), frozen_at=NOW,
        date_policy={"mode": "custom", "start": "2026-09-01", "end": "2026-09-07"})
    assert "2026-09-01 through 2026-09-07 inclusive" in rendered.raw_bytes.decode()


def test_v2_envelope_preserves_original_artifact_pointer_and_bytes(tmp_path):
    original = payload()
    path = tmp_path / "discovery.json"
    raw = json.dumps(original).encode()
    path.write_bytes(raw)
    accepted = _read_pillar_b(path, report_date=DAY.isoformat())
    candidates = adapt_pillar_b(accepted, artifact_id=path.name,
        artifact_sha256=hashlib.sha256(raw).hexdigest(), discovered_at=NOW)
    assert candidates[0].origins[0].row == "/articles/0"
    assert candidates[0].origins[0].source == "Research Institute"
    assert accepted == original
    assert path.read_bytes() == raw


def test_unlimited_keeps_old_and_unknown_but_window_filters_selection_only():
    value = payload()
    unknown = deepcopy(value["articles"][0])
    unknown.update(url="https://example.org/unknown", published_date=None, date_evidence=None)
    value["articles"].append(unknown)
    assert len(validate_discovery(value, report_date=DAY)) == 2

    value["date_policy"] = policy({"mode": "custom", "start": "2026-09-01", "end": "2026-09-07"})
    assert validate_discovery(value, report_date=DAY) == []
    assert len(validate_discovery(value, report_date=DAY, retain_ineligible=True)) == 2
    for boundary in ("2026-09-01", "2026-09-07"):
        value["articles"][0]["published_date"] = boundary
        assert len(validate_discovery(value, report_date=DAY)) == 1


@pytest.mark.parametrize("kind", ["event", "issue", "fetch"])
def test_non_publication_date_evidence_is_rejected(kind):
    value = payload()
    value["articles"][0]["date_evidence"]["kind"] = kind
    with pytest.raises(ValueError, match="date evidence"):
        validate_discovery(value, report_date=DAY)


def test_arbitrary_queries_success_failure_and_no_search_are_truthful():
    value = payload()
    value["searches"][0].update(status="failed", error="timeout", result_refs=[])
    with pytest.raises(PillarBIncompleteError, match="not zero results"):
        validate_discovery(value, report_date=DAY)
    value["articles"] = []
    assert validate_discovery(value, report_date=DAY, allow_incomplete=True) == []

    value.update(search_decision={"status": "no_search", "reason": "complete site coverage"}, searches=[])
    assert validate_discovery(value, report_date=DAY) == []
    value["search_decision"]["reason"] = ""
    with pytest.raises(ValueError, match="no_search"):
        validate_discovery(value, report_date=DAY)


def test_search_articles_must_link_to_result_from_the_named_successful_attempt():
    value = payload()
    value["articles"][0]["result_ref"] = "https://not-returned.example/result"
    with pytest.raises(ValueError, match="successful search attempt"):
        validate_discovery(value, report_date=DAY)

    value = payload()
    value["searches"].append({**value["searches"][0], "search_ref": "search-2",
                              "status": "failed", "result_refs": ["failed-result"],
                              "error": "timeout"})
    value["articles"][0].update(search_ref="search-2", result_ref="failed-result")
    with pytest.raises(ValueError, match="successful search attempt"):
        validate_discovery(value, report_date=DAY, allow_incomplete=True)

    value = payload()
    value["articles"][0]["search_ref"] = "unknown-search"
    with pytest.raises(ValueError, match="successful search attempt"):
        validate_discovery(value, report_date=DAY)


@pytest.mark.parametrize("field,value,message", [
    ("attempted_at", "yesterday", "attempted_at"),
    ("result_refs", ["", "valid"], "result_refs"),
    ("result_refs", ["same", "same"], "result_refs"),
    ("budget", {}, "budget"),
    ("budget", {"max_results": True}, "budget"),
    ("budget", {"max_results": -1}, "budget"),
])
def test_pillar_b_search_attempt_fields_are_strict(field, value, message):
    value_payload = payload()
    value_payload["searches"][0][field] = value
    with pytest.raises(ValueError, match=message):
        validate_discovery(value_payload, report_date=DAY)


def test_storage_phase_can_read_failed_search_without_claiming_zero_results(tmp_path):
    value = payload()
    value["searches"][0].update(status="failed", error="timeout", result_refs=[])
    value["articles"] = []
    path = tmp_path / "failed-search.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(SystemExit, match="not zero results"):
        _read_pillar_b(path, report_date=DAY.isoformat())
    assert _read_pillar_b(
        path, report_date=DAY.isoformat(), allow_incomplete=True
    ) == value


def test_v1_is_explicitly_rejected_not_fabricated():
    value = payload()
    value["schema_version"] = "pillar-b-discovery.v1"
    with pytest.raises(ValueError, match="incompatible"):
        validate_discovery(value, report_date=DAY)
