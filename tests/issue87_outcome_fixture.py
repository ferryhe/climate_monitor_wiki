"""Closed fixture allowlist for dependency-free subprocess tests, not a v2 schema."""
import json
from pathlib import Path

FIXTURES = tuple(Path(__file__).with_name('fixtures').joinpath('issue87', name) for name in (
    'acquisition_batch_result.v2.57.json', 'acquisition_batch_result.v2.2.json',
    'wri_repro/acquisition-batch-result.v2.json'))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate fixture key')
        result[key] = value
    return result


def validate_fixture(raw: str) -> dict:
    """Accept only exact saved public outputs; reject even valid unseen batches."""
    payload = json.loads(raw, object_pairs_hook=_unique_object)
    canonical = json.dumps(payload, sort_keys=True, allow_nan=False)
    for path in FIXTURES:
        expected = json.loads(path.read_text(encoding='utf-8'))
        if canonical == json.dumps(expected, sort_keys=True, allow_nan=False):
            return payload
    raise ValueError('outcome is not an approved saved test fixture')
