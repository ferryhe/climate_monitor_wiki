"""Issue #87 post-pr106 follow-ups: Pillar B source contract + argv-safe Hermes.

AC-1: the Pillar B producer's ``source`` field is an institution/website name
(e.g. "California Department of Insurance", "Ceres"), not a literal "web"
keyword. Both preflight (``_read_pillar_b``) and the consumer
(``adapt_pillar_b``) must accept institution sources and preserve provenance.

AC-2: server hermes 03fa32c exposes only ``--query`` (no ``--query-file``); the
~9.6 MB composed authoring instruction must be delivered through the argv-safe
``--query -`` stdin channel, never as a raw argv element.
"""
from __future__ import annotations

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
# AC-2: argv-safe Hermes authoring channel (03fa32c --query only)
# ---------------------------------------------------------------------------


def _help_03fa32c() -> str:
    # Boss's SSH audit (issue comment 5575930257): 03fa32c advertises --query
    # only, no --query-file.
    return "usage: hermes chat ...\n  -q QUERY, --query QUERY  Single query (non-interactive mode)\n"


def _help_with_query_file() -> str:
    # A newer Hermes advertises --query-file; the production entrypoint must
    # still ignore it because the real server 03fa32c does not expose it.
    return (
        "usage: hermes chat ...\n"
        "  -q QUERY, --query QUERY  Single query (non-interactive mode)\n"
        "  --query-file PATH  Read the single query from a file instead of the command line\n"
    )


def test_ac2_invocation_keeps_full_instruction_out_of_argv():
    instruction = "A" * 9_643_411  # ~9.6 MB composed instruction
    command, stdin = monitor._hermes_authoring_invocation(
        _help_03fa32c(), instruction, model="gpt-6-astra", provider="openai-codex"
    )
    # The full instruction must never be an argv element.
    assert instruction not in command
    assert all(len(arg) < 4096 for arg in command)
    assert command[command.index("--query") + 1] == "-"
    assert "--query-file" not in command
    assert command[command.index("--toolsets") + 1] == "none"
    assert command[command.index("--model") + 1] == "gpt-6-astra"
    assert command[command.index("--provider") + 1] == "openai-codex"
    # The full instruction is delivered over stdin, byte-for-byte (no shrink,
    # truncate, or split).
    assert stdin == instruction
    assert len(stdin.encode("utf-8")) == len(instruction.encode("utf-8"))


def test_ac2_ignores_query_file_even_when_advertised():
    instruction = "composed request + evidence"
    command, stdin = monitor._hermes_authoring_invocation(
        _help_with_query_file(), instruction
    )
    # --query-file is never selected, even when the help advertises it, because
    # 03fa32c does not expose it (avoids silent argv downgrade on the server).
    assert "--query-file" not in command
    assert command[command.index("--query") + 1] == "-"
    assert stdin == instruction
    assert instruction not in command


def test_ac2_missing_query_capability_fails_closed():
    with pytest.raises(SystemExit, match="query capability unavailable"):
        monitor._hermes_authoring_invocation("usage: hermes chat ...\n  --image IMAGE\n", "x")
