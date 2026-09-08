"""Issue #87 post-pr106 follow-ups: Pillar B source contract + argv-safe Hermes.

AC-1: the Pillar B producer's ``source`` field is an institution/website name
(e.g. "California Department of Insurance", "Ceres"), not a literal "web"
keyword. Both preflight (``_read_pillar_b``) and the consumer
(``adapt_pillar_b``) must accept institution sources and preserve provenance.

AC-2: the ~9.6 MB authoring instruction requires the explicit ``--query-file -``
stdin channel. The old ``--query -`` sends a literal dash instead of evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from climate_monitor.article_candidate_contract import (
    CandidateContractError,
    adapt_pillar_b,
)
from scripts import run_climate_monitor as monitor

# Committed 16-row Pillar B fixture (byte-identical to the real producer
# output captured on 2026-09-07 by issue #87 owner, ZIP
# `85c1492c…`). Living inside the repo so CI exercises the AC-1 contract on
# every run; if this file disappears or drifts from the owner SHA, the
# tests must fail, not silently skip.
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "issue87" / "pillar_b_sample_16.json"
FIXTURE_RELATIVE_ID = "pillar_b_sample_16.json"
OWNER_FIXTURE_SHA256 = "e07388cb37c8d258b7555325cca751a7d36b967ea290821370728a95febd2c6c"


def _read_fixture() -> tuple[list[dict], bytes]:
    if not FIXTURE_PATH.is_file():
        pytest.fail(
            f"committed Pillar B fixture missing: {FIXTURE_PATH} "
            "(AC-1 regression coverage requires the 16-row sample in-tree)"
        )
    raw = FIXTURE_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != OWNER_FIXTURE_SHA256:
        pytest.fail(
            f"committed Pillar B fixture SHA drifted from owner ZIP: "
            f"{FIXTURE_PATH} (expected {OWNER_FIXTURE_SHA256})"
        )
    return json.loads(raw), raw


# ---------------------------------------------------------------------------
# AC-1: Pillar B institution source contract
# ---------------------------------------------------------------------------


def test_ac1_consumer_accepts_institution_sources_and_preserves_provenance():
    payload, raw = _read_fixture()
    fixture_sha = hashlib.sha256(raw).hexdigest()
    candidates = adapt_pillar_b(
        payload,
        artifact_id=FIXTURE_RELATIVE_ID,
        artifact_sha256=fixture_sha,
        discovered_at="2026-09-07T00:00:00Z",
    )
    # 16 rows -> 16 distinct-URL candidates, no accidental merge/drop.
    assert len(candidates) == 16

    # ``merge_candidates`` deterministically orders candidates by canonical
    # URL, so the output index is NOT the input row index. We therefore assert
    # the (row, source) multiset as a whole: every input row appears in exactly
    # one candidate's origins with its original source value, and no extra rows
    # leak through. A naive ``set`` comparison would let a permutation of wrong
    # sources still pass; pinning the multiset closes that hole (Copilot
    # review feedback on PR #107).
    expected_pairs = sorted(
        (f"/{i}", item["source"]) for i, item in enumerate(payload)
    )
    actual_pairs = sorted(
        (origin.row, origin.source)
        for candidate in candidates
        for origin in candidate.origins
    )
    assert actual_pairs == expected_pairs
    assert "California Department of Insurance" in {s for _, s in actual_pairs}
    assert "Ceres" in {s for _, s in actual_pairs}

    # Every candidate origin pins the committed fixture identity and keeps the
    # 16-row row pointer space /0../15.
    assert {origin.row for c in candidates for origin in c.origins} == {
        f"/{i}" for i in range(16)
    }
    for candidate in candidates:
        for origin in candidate.origins:
            assert origin.pillar == "B"
            assert origin.input_artifact.artifact_id == FIXTURE_RELATIVE_ID
            assert origin.input_artifact.sha256 == fixture_sha


def test_ac1_preflight_and_consumer_parity_on_committed_fixture():
    payload, raw = _read_fixture()
    fixture_sha = hashlib.sha256(raw).hexdigest()
    # Both the preflight reader and the real consumer accept the same 16-row
    # fixture (institution sources), so they no longer disagree.
    assert len(monitor._read_pillar_b(FIXTURE_PATH)) == 16
    assert len(adapt_pillar_b(
        payload,
        artifact_id=FIXTURE_RELATIVE_ID,
        artifact_sha256=fixture_sha,
        discovered_at="2026-09-07T00:00:00Z",
    )) == 16


@pytest.mark.parametrize("mutation", ["missing", "empty", "whitespace"])
def test_ac1_preflight_and_consumer_reject_bad_source_together(tmp_path, mutation):
    base = {"title": "x", "url": "https://example.org/a", "summary": "s"}
    if mutation == "missing":
        item = {"title": base["title"], "url": base["url"], "summary": base["summary"]}
    elif mutation == "empty":
        item = {**base, "source": ""}
    else:
        item = {**base, "source": "   "}
    path = tmp_path / "pillar_b_bad.json"
    path.write_text(json.dumps([item]))

    with pytest.raises(SystemExit):
        monitor._read_pillar_b(path)
    with pytest.raises(CandidateContractError):
        adapt_pillar_b(
            json.loads(path.read_text()),
            artifact_id="x.json",
            artifact_sha256="a" * 64,
            discovered_at="2026-09-07T00:00:00Z",
        )


# ---------------------------------------------------------------------------
# AC-2: explicit Hermes stdin capability
# ---------------------------------------------------------------------------


def _help_03fa32c() -> str:
    # The installed 03fa32c parser has no file/stdin input option.
    return "usage: hermes chat ...\n  -q QUERY, --query QUERY  Single query (non-interactive mode)\n"


def _help_with_query_file() -> str:
    # Verified against the public v2026.9.7 command parser on the server.
    return (
        "usage: hermes chat ...\n"
        "  -q QUERY, --query QUERY  Single query (non-interactive mode)\n"
        "  --query-file PATH  Read the single query from a file instead of the command line\n"
        "  --max-turns N\n  --reasoning EFFORT\n  --ignore-rules\n"
    )


def test_ac2_invocation_keeps_full_instruction_out_of_argv():
    instruction = "正文\n$(literal) `literal`\n" + "A" * 9_643_411
    command, stdin = monitor._hermes_authoring_invocation(
        _help_with_query_file(), instruction, model="gpt-6-astra", provider="openai-codex"
    )
    # The full instruction must never be an argv element.
    assert instruction not in command
    assert all(len(arg) < 4096 for arg in command)
    assert command[command.index("--query-file") + 1] == "-"
    assert "--query" not in command
    assert command[command.index("--toolsets") + 1] == "none"
    assert command[command.index("--max-turns") + 1] == "1"
    assert command[command.index("--reasoning") + 1] == "none"
    assert "--ignore-rules" in command
    assert command[command.index("--model") + 1] == "gpt-6-astra"
    assert command[command.index("--provider") + 1] == "openai-codex"
    # The full instruction is delivered over stdin, byte-for-byte (no shrink,
    # truncate, or split).
    assert stdin == instruction
    assert len(stdin.encode("utf-8")) == len(instruction.encode("utf-8"))


def test_ac2_file_capability_does_not_require_query_option():
    instruction = "composed request + evidence"
    command, stdin = monitor._hermes_authoring_invocation(
        "usage: hermes chat\n  --query-file PATH\n  --max-turns N\n  --reasoning EFFORT\n  --ignore-rules\n", instruction
    )
    assert command[command.index("--query-file") + 1] == "-"
    assert "--query" not in command
    assert stdin == instruction
    assert instruction not in command


@pytest.mark.parametrize("help_stdout", [_help_03fa32c(), "usage: hermes chat\n --image IMAGE\n"])
def test_ac2_missing_stdin_capability_fails_closed(help_stdout):
    with pytest.raises(SystemExit, match="requires --query-file support"):
        monitor._hermes_authoring_invocation(help_stdout, "full authoring evidence")


def test_verified_runtime_startup_notice_preserves_complete_json():
    notice = (
        "  ⚠ tirith security scanner enabled but not available — "
        "command scanning will use pattern matching only\n"
    )
    payload = {"summary": "正文", "literal_notice": notice}
    stdout = "Warning: Unknown toolsets: none\n" + notice + json.dumps(payload)
    stderr = "\nsession_id: 20260908_074019_df4b08\n"
    assert monitor._parse_hermes_quiet_response(stdout, stderr) == payload
    with pytest.raises(ValueError):
        monitor._parse_hermes_quiet_response(notice + stdout, stderr)


def test_hermes_json_fence_is_framing_not_a_repair():
    payload = {'summary': 'Literal ``` content remains intact.'}
    raw = json.dumps(payload)
    stderr = '\nsession_id: 20260908_120234_3b2f4b\n'
    assert monitor._parse_hermes_quiet_response('Warning: Unknown toolsets: none\n```json\n' + raw + '\n```\n', stderr) == payload
    for invalid in ['Explanation\n```json\n' + raw + '\n```',
                    '```json\n' + raw[:-1] + '\n```',
                    '```json\n' + raw + '\n```\nUnexpected suffix', raw + raw]:
        with pytest.raises(ValueError):
            monitor._parse_hermes_quiet_response(invalid, stderr)


@pytest.mark.parametrize("missing", ["--max-turns", "--reasoning", "--ignore-rules"])
def test_authoring_requires_runtime_turn_controls(missing):
    with pytest.raises(SystemExit, match="bounded-turn and reasoning controls"):
        monitor._hermes_authoring_invocation(_help_with_query_file().replace(missing, ""), "request")


def _view_evidence(body, content_type="text/plain"):
    return {"artifact_digest": "source-artifact", "records": [{
        "article_id": "article-1", "requested_url": "https://example.org/article",
        "final_url": "https://example.org/article", "status": "ok",
        "content_type": content_type, "content": body,
        "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "record_hash": "source-record", "extra": {"search_snippet": "source snippet"},
    }]}


def test_authoring_text_view_preserves_complete_plain_text_and_source():
    body = "正文 beginning\n" + "full source paragraph\n" * 10000 + "END OF SOURCE"
    evidence = _view_evidence(body)
    original = copy.deepcopy(evidence)
    view = monitor._authoring_evidence_view(evidence)
    text = view["records"][0]["readable_content"]
    assert text["text"] == body
    assert text["text_sha256"] == text["source_content_hash"]
    assert view["source_artifact_digest"] == evidence["artifact_digest"]
    assert view["records"][0]["source_record_hash"] == "source-record"
    assert evidence == original


def test_authoring_html_uses_upstream_full_markdown_without_cutoff():
    normalizer = pytest.importorskip("web_listening.blocks.normalizer")
    body = ("<html><script>irrelevant_script_payload</script><body><main><h1>Policy</h1>"
            + "<p>Full climate insurance paragraph.</p>" * 1000
            + '<table><tr><th>Year</th><th>Loss</th></tr><tr><td>2026</td><td>42</td></tr></table>'
            + '<p><a href="/source">Citation</a> END OF ARTICLE</p></main></body></html>')
    evidence = _view_evidence(body, "text/html; charset=utf-8")
    original = copy.deepcopy(evidence)
    text = monitor._authoring_evidence_view(evidence)["records"][0]["readable_content"]
    assert text["text"] == normalizer.normalize_html(body, "https://example.org/article").markdown
    assert "END OF ARTICLE" in text["text"]
    assert "https://example.org/source" in text["text"]
    assert "2026" in text["text"] and "42" in text["text"]
    assert "irrelevant_script_payload" not in text["text"]
    assert text["source_content_hash"] == evidence["records"][0]["content_hash"]
    assert text["text_sha256"] == hashlib.sha256(text["text"].encode()).hexdigest()
    assert text["text_sha256"] != text["source_content_hash"]
    assert evidence == original


def test_authoring_view_rejects_content_hash_mismatch():
    evidence = _view_evidence("verified body")
    evidence["records"][0]["content"] = "changed body"
    with pytest.raises(ValueError, match="source content hash mismatch"):
        monitor._authoring_evidence_view(evidence)


def test_authoring_view_keeps_missing_body_and_snippet_honest():
    evidence = _view_evidence("")
    evidence["records"][0].update(status="unavailable", content=None, content_hash=None)
    view = monitor._authoring_evidence_view(evidence)
    assert view["records"][0]["readable_content"] is None
    assert view["records"][0]["status"] == "unavailable"
    assert view["records"][0]["search_snippet"] == "source snippet"


@pytest.mark.parametrize('climate,actuarial,expected', [(True, True, True), (True, False, False), (False, True, False)])
def test_single_url_relevance_requires_both_decisions(climate, actuarial, expected):
    from datetime import date
    from climate_monitor.models import CandidateItem
    evidence = _view_evidence('Insurance supervision applies climate scenarios to solvency risk.')
    record = evidence['records'][0]
    record['content_ref'] = 'memory:' + record['content_hash']
    item = CandidateItem(title='Climate supervision', url=record['requested_url'],
                         summary='', source_name='', lane='website')
    request = monitor.build_authoring_request(report_date=date(2026, 9, 7), items=[item],
        prompt=monitor.load_weekly_monitor_prompt(), article_evidence=evidence,
        stats={'total': 1, 'updated': 1, 'unchanged': 0, 'blocked': 0, 'failed': 0, 'unresolved': 0})
    raw = dict(climate_related=climate, actuarial_related=actuarial,
        summary='Insurance supervision applies climate scenarios to solvency risk.',
        summary_basis='article_content', evidence_hash=record['content_hash'],
        categories=['Supervision & Disclosure'], keywords=['insurance', 'supervision', 'solvency'])
    result = monitor._validate_url_authoring(raw, request['articles'][0], request, item, monitor.load_article_taxonomy())
    assert result['relevant'] is expected
    assert result['article_id'] == request['articles'][0]['article_id']


def test_checkpoint_reuse_never_calls_model_or_accepts_changed_input(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import subprocess
    calls = []
    def model(command, **kwargs):
        calls.append(kwargs['input'])
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr='\nsession_id: 20260908_120234_3b2f4b\n')
    monkeypatch.setattr(subprocess, 'run', model)
    args = SimpleNamespace(model='test-model', model_provider='test-provider', authoring_timeout=1)
    path = tmp_path / 'checkpoint.json'
    options = dict(args=args, help_stdout=_help_with_query_file(), validate=lambda raw: raw['ok'])
    assert monitor._checkpointed_authoring(path, 'one URL evidence', **options) is True
    original = path.read_bytes()
    assert monitor._checkpointed_authoring(path, 'one URL evidence', **options) is True
    with pytest.raises(SystemExit, match='checkpoint input changed'):
        monitor._checkpointed_authoring(path, 'changed URL evidence', **options)
    assert calls == ['one URL evidence']
    assert path.read_bytes() == original


def test_resume_supplies_only_this_items_validation_error_in_a_fresh_request(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import subprocess
    calls = []
    def model(command, **kwargs):
        calls.append(kwargs['input'])
        corrected = 'Prior validation error' in kwargs['input']
        return SimpleNamespace(returncode=0, stdout=json.dumps({'summary': '' if corrected else 'No content available.'}),
                               stderr='\nsession_id: 20260908_120234_3b2f4b\n')
    def validate(raw):
        if raw['summary']:
            raise ValueError('summary_basis none cannot carry summary evidence')
        return raw
    monkeypatch.setattr(subprocess, 'run', model)
    args = SimpleNamespace(model='test-model', model_provider='test-provider', authoring_timeout=1)
    path = tmp_path / 'checkpoint.json'
    guidance = 'This URL has no body or snippet; summary must be empty.'
    options = dict(args=args, help_stdout=_help_with_query_file(), validate=validate,
                   retry_guidance=guidance)
    instruction = 'Analyze only https://example.test/one; evidence is unavailable.'
    with pytest.raises(ValueError, match='authoring item failed'):
        monitor._checkpointed_authoring(path, instruction, **options)
    failed = json.loads(path.read_text())
    assert monitor._checkpointed_authoring(path, instruction, **options) == {'summary': ''}
    assert len(calls) == 2
    assert calls[0] == instruction
    assert calls[1].endswith(instruction)
    assert guidance in calls[1] and guidance not in calls[0]
    assert 'summary_basis none cannot carry summary evidence' in calls[1]
    saved = json.loads(path.read_text())
    assert saved['input_sha256'] == failed['input_sha256']
    assert saved['request_sha256'] == hashlib.sha256(calls[1].encode()).hexdigest()
    assert monitor._checkpointed_authoring(path, instruction, **options) == {'summary': ''}
    assert len(calls) == 2
