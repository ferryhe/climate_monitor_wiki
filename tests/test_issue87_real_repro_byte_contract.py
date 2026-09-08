"""Real Pillar B producer byte-identity contract (SSH-sandbox / local only).

This file pins the producer's 16-row byte-identical output captured on
2026-09-07. It is *not* a CI test: CI must not depend on out-of-tree producer
artefacts. Locally (and inside the SSH sandbox that generated the file) run:

    REAL_REPRO_PATH=/tmp/issue87-pr106-repro/pillar_b_20260907.json \\
        python -m pytest tests/test_issue87_real_repro_byte_contract.py -v

Without ``REAL_REPRO_PATH`` pointing at the byte-identical file the entire
module is skipped, so the standard ``pytest -q`` CI invocation stays green
while still preserving the producer contract regression.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from climate_monitor.article_candidate_contract import adapt_pillar_b

# Pinned from the producer output captured during PR #106 follow-up. Update
# this constant deliberately only when the producer changes shape; bump the
# contract version together with it.
REAL_REPRO_PATH = Path(
    os.environ.get(
        "REAL_REPRO_PATH",
        "/tmp/issue87-pr106-repro/pillar_b_20260907.json",
    )
)
EXPECTED_SHA256 = "e07388cb37c8d258b7555325cca751a7d36b967ea290821370728a95febd2c6c"
EXPECTED_ROW_COUNT = 16


pytestmark = pytest.mark.skipif(
    not REAL_REPRO_PATH.is_file(),
    reason=(
        "real Pillar B producer byte-identical file not present "
        f"(set REAL_REPRO_PATH={REAL_REPRO_PATH} to enable; CI runs the "
        "committed fixture in tests/test_issue87_post_pr106.py instead)"
    ),
)


def _load_real_repro() -> tuple[list[dict], bytes]:
    raw = REAL_REPRO_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA256, (
        f"producer drift: {REAL_REPRO_PATH} sha256 != pinned contract; "
        "bump EXPECTED_SHA256 + EXPECTED_ROW_COUNT together and re-baseline."
    )
    payload = json.loads(raw)
    assert len(payload) == EXPECTED_ROW_COUNT
    return payload, raw


def test_real_repro_preflight_returns_sixteen_rows():
    from scripts import run_climate_monitor as monitor

    payload, raw = _load_real_repro()
    assert len(monitor._read_pillar_b(REAL_REPRO_PATH)) == EXPECTED_ROW_COUNT


def test_real_repro_consumer_preserves_row_to_source_mapping():
    payload, raw = _load_real_repro()
    sha = hashlib.sha256(raw).hexdigest()
    candidates = adapt_pillar_b(
        payload,
        artifact_id=REAL_REPRO_PATH.name,
        artifact_sha256=sha,
        discovered_at="2026-09-07T00:00:00Z",
    )
    assert len(candidates) == EXPECTED_ROW_COUNT

    # ``merge_candidates`` deterministically orders candidates by canonical
    # URL, so the output index is NOT the input row index. Assert the
    # (row, source) multiset as a whole so a permutation of wrong sources
    # cannot pass.
    expected_pairs = sorted(
        (f"/{i}", item["source"]) for i, item in enumerate(payload)
    )
    actual_pairs = sorted(
        (origin.row, origin.source)
        for candidate in candidates
        for origin in candidate.origins
    )
    assert actual_pairs == expected_pairs
