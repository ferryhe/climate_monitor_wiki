import pytest

from scripts.run_agent_acquisition import _candidate_transcript_result


def test_hermes_loop_diagnostic_does_not_corrupt_candidate_json():
    payload = '{"status":"failed","body_status":"failed"}'
    warning = '\n\n[Tool loop warning: same_tool_failure_warning; count=3; climate_stage_candidate has failed 3 times this turn. Diagnose before retrying.]'
    assert _candidate_transcript_result(payload + warning) == {
        'status': 'failed', 'body_status': 'failed'
    }


@pytest.mark.parametrize('value', [
    '{"status":"failed"}\n\nUnrelated appended content',
    '{"status":"failed"}\n\n[Tool loop warning: broken',
    '{invalid}\n\n[Tool loop warning: example]',
])
def test_unrecognized_or_broken_transcript_is_not_accepted(value):
    assert _candidate_transcript_result(value) == value
