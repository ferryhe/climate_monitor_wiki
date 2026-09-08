from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from climate_monitor.models import CandidateItem
from climate_monitor.weekly_monitor.prompt_loader import load_article_relevance_rules
from scripts import run_climate_monitor as monitor


def test_relevance_rules_can_be_maintained_independently(tmp_path):
    path = tmp_path / "rules.md"
    path.write_text("Custom domain rules", encoding="utf-8")
    assert load_article_relevance_rules(path) == "Custom domain rules"
    path.write_text(" \n", encoding="utf-8")
    with pytest.raises(ValueError, match="rules are empty"):
        load_article_relevance_rules(path)


@pytest.mark.parametrize('text', [
    '- Flood losses affect insurance pricing.',
    '* Flood losses affect insurance pricing.',
    '+ Flood losses affect insurance pricing.',
    '1. Flood losses affect insurance pricing.',
    '# Climate findings',
    '> Flood losses affect insurance pricing.',
    '```text\nFlood losses affect insurance pricing.\n```',
])
def test_executive_rejects_blocks_before_checkpointing(text):
    with pytest.raises(ValueError, match='prose paragraphs'):
        monitor._validate_executive_authoring({'executive_summary': text})


def test_url_driver_embeds_rules_in_one_request_and_skips_excluded_summary(tmp_path, monkeypatch):
    from tests.test_issue87_post_pr106 import _view_evidence, _help_with_query_file
    import subprocess

    evidence = _view_evidence("A generic environmental article with no insurance connection.")
    item = CandidateItem(title="Environmental news", url=evidence["records"][0]["requested_url"],
                         summary="", source_name="", lane="website")
    request = monitor.build_authoring_request(
        report_date=date(2026, 9, 7), items=[item], prompt=monitor.load_weekly_monitor_prompt(),
        article_evidence=evidence,
        stats={"total": 1, "updated": 1, "unchanged": 0, "blocked": 0, "failed": 0, "unresolved": 0})
    for name, payload in (("article_evidence.json", evidence), ("v2_authoring_request.json", request)):
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "bundle.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(monitor, "_verify_authoring_resume", lambda *args: None)
    # History binding is covered by the production-chain resume tests; this
    # fixture isolates prompt composition and the excluded-article response.
    monkeypatch.setattr(monitor, "_verify_candidate_selection", lambda *args: None)
    monkeypatch.setattr(monitor, "_read_staging_bundle", lambda path: {})
    monkeypatch.setattr(monitor, "_run_finalize", lambda *args: 0)
    calls = []

    def hermes(command, **kwargs):
        if "--help" in command:
            return SimpleNamespace(returncode=0, stdout=_help_with_query_file())
        calls.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stderr="\nsession_id: 20260908_120234_3b2f4b\n",
                               stdout=json.dumps({"climate_related": True, "actuarial_related": False,
                                   "summary": "", "summary_basis": "none", "evidence_hash": None,
                                   "categories": [], "keywords": []}))

    monkeypatch.setattr(subprocess, "run", hermes)
    args = SimpleNamespace(staging_dir=str(tmp_path), model="fixture-model",
                           model_provider="fixture-provider", authoring_timeout=5)
    assert monitor._run_authoring_sequence(args, None) == 0
    assert len(calls) == 1
    assert load_article_relevance_rules() in calls[0]
    response = json.loads((tmp_path / "authoring_response.json").read_text())
    assert response["articles"][0]["relevant"] is False
    assert response["articles"][0]["summary"] == response["executive_summary"] == ""
