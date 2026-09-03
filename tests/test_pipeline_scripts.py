"""Focused contract tests for the new weekly pipeline scripts (scripts/step*.py).

These tests exercise the scripts as subprocesses against a temp data
directory (CLIMATE_REPORTS_DIR), so they stay independent of the production
/home/ubuntu paths.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def run_script(name, *args, env_extra=None, cwd=REPO):
    env = dict(os.environ)
    env.setdefault("CLIMATE_WIKI_HOME", str(REPO))
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [PYTHON, str(REPO / "scripts" / name), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=60,
    )


@pytest.fixture()
def reports_dir(tmp_path):
    d = tmp_path / "data" / "reports"
    d.mkdir(parents=True)
    return d


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False))


def test_step3_filter_joins_assessments_by_url(reports_dir):
    """A dropped non-relevant item must not shift summaries onto other URLs."""
    write_json(reports_dir / "aggregated_2026-09-14.json", {
        "date": "2026-09-14",
        "items": [
            {"title": "Climate flood report", "url": "https://example.org/a", "source": "OrgA", "pillar": "A"},
            {"title": "Board appointment", "url": "https://example.org/b", "source": "OrgA", "pillar": "A"},
            {"title": "Parametric launch", "url": "https://example.org/c", "source": "OrgB", "pillar": "A"},
        ],
    })
    write_json(reports_dir / "hermes_assessments_2026-09-14.json", {
        "assessments": [
            {"id": 0, "url": "https://example.org/a", "relevant": True,
             "category": "catastrophe_natcat", "summary": "grounded summary A", "keywords": ["flood"]},
            {"id": 1, "url": "https://example.org/b", "relevant": False,
             "category": "general", "summary": "", "keywords": []},
            {"id": 2, "url": "https://example.org/c", "relevant": True,
             "category": "parametric_insurance", "summary": "grounded summary C", "keywords": ["parametric"]},
        ],
    })
    result = run_script("step3_filter.py", "--date", "2026-09-14",
                        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)})
    assert result.returncode == 0, result.stdout + result.stderr
    items = json.loads((reports_dir / "filtered_2026-09-14.json").read_text())["items"]
    assert [i["url"] for i in items] == ["https://example.org/a", "https://example.org/c"]
    assert items[0]["summary"] == "grounded summary A"
    assert items[1]["summary"] == "grounded summary C"
    assert items[1]["category"] == "parametric_insurance"


def test_step5_requires_consistent_monitor_stats(reports_dir):
    write_json(reports_dir / "filtered_2026-09-14.json", {
        "total_input": 1, "relevant": 1, "non_relevant": 0,
        "items": [{"title": "t", "url": "https://example.org/a", "category": "general",
                   "summary": "", "keywords": [], "source": "OrgA", "pillar": "A"}],
    })
    write_json(reports_dir / "hermes_assessments_2026-09-14.json", {
        "assessments": [], "executive_summary": "test summary"})
    env = {"CLIMATE_REPORTS_DIR": str(reports_dir)}
    allow = ["--allow-future", "--allow-offcycle"]

    # Missing fields must fail (no fabricated zeros).
    write_json(reports_dir / "stats.json", {"checked": 25})
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        "--monitor-stats", str(reports_dir / "stats.json"),
                        *allow, env_extra=env)
    assert result.returncode == 1

    # Inconsistent totals must fail.
    write_json(reports_dir / "stats.json", {"checked": 57, "succeeded": 54, "failed": 1})
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        "--monitor-stats", str(reports_dir / "stats.json"),
                        *allow, env_extra=env)
    assert result.returncode == 1

    # Consistent stats succeed and are written verbatim.
    write_json(reports_dir / "stats.json", {"checked": 57, "succeeded": 54, "failed": 3})
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        "--monitor-stats", str(reports_dir / "stats.json"),
                        *allow, env_extra=env)
    assert result.returncode == 0, result.stdout + result.stderr
    md = (reports_dir / "climate-monitor-2026-09-14.md").read_text()
    assert "Sites checked: **57**, succeeded: **54**, failed: **3**" in md
    assert (reports_dir / "climate-monitor-2026-09-14.json").exists()


@pytest.mark.parametrize("script", [
    "step2_save_state.py", "step3_aggregate.py", "step3_filter.py",
    "step5_build_md.py", "step6_render_pdf.py", "step7b_extract_conferences.py",
    "step9_update_website.py",
])
def test_scripts_reject_path_traversal_dates(script):
    result = run_script(script, "--date", "../etc/passwd")
    assert result.returncode == 1
    assert "invalid --date" in result.stdout


def test_step5_rejects_future_dates(reports_dir):
    result = run_script("step5_build_md.py", "--date", "2099-01-04",
                        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)})
    assert result.returncode == 1
    assert "in the future" in result.stdout


def test_step3b_generate_refuses_to_overwrite_without_force(reports_dir):
    write_json(reports_dir / "aggregated_2026-09-14.json",
               {"date": "2026-09-14", "items": []})
    write_json(reports_dir / "hermes_assessments_2026-09-14.json",
               {"assessments": [], "executive_summary": "llm produced this"})
    result = run_script("step3b_generate_assessments.py", "--date", "2026-09-14",
                        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)})
    assert result.returncode == 0
    assert "SKIP" in result.stdout
    payload = json.loads((reports_dir / "hermes_assessments_2026-09-14.json").read_text())
    assert payload["executive_summary"] == "llm produced this"


def test_step2_save_state_writes_pillar_b_urls(reports_dir):
    """Regression: the script once crashed on an undefined pb_path NameError."""
    state_dir = reports_dir / "state"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "article_state.json"
    write_json(reports_dir / "pillar_b_2026-09-14.json", [
        {"url": "https://example.org/b1", "title": "t1"},
        {"url": "https://example.org/b1", "title": "dup"},
        {"url": "https://example.org/b2/", "title": "t2"},
    ])
    write_json(state_file, {"__pillar_b__": ["https://example.org/b1"]})
    result = run_script("step2_save_state.py", "--date", "2026-09-14",
                        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir),
                                   "CLIMATE_WL_STATE": str(state_file)})
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(state_file.read_text())
    # b1 was already present and duplicated; b2 is new (trailing slash stripped).
    assert state["__pillar_b__"] == ["https://example.org/b1", "https://example.org/b2"]


def test_step3_filter_keyword_fallback_single_string_category_and_both_keywords(reports_dir):
    """Keyword fallback: category must be a str, and relevance needs BOTH
    climate and actuarial keywords (matching the Step 3b prompt contract)."""
    write_json(reports_dir / "aggregated_2026-09-14.json", {
        "date": "2026-09-14",
        "items": [
            {"title": "Flood insurance claims surge", "url": "https://example.org/flood",
             "source": "OrgA", "pillar": "A"},
            {"title": "Solar energy report", "url": "https://example.org/solar",
             "source": "OrgA", "pillar": "A"},
            {"title": "Board appointment", "url": "https://example.org/board",
             "source": "OrgA", "pillar": "A"},
        ],
    })
    result = run_script("step3_filter.py", "--date", "2026-09-14",
                        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)})
    assert result.returncode == 0, result.stdout + result.stderr
    items = json.loads((reports_dir / "filtered_2026-09-14.json").read_text())["items"]
    assert [i["url"] for i in items] == ["https://example.org/flood"]
    assert isinstance(items[0]["category"], str)
    assert items[0]["category"] == "catastrophe_natcat"


def test_step3b_fallback_relevance_and_conference_survival(reports_dir):
    """Fallback assessments: BOTH keyword families required; pre-identified
    conference URLs stay relevant so step7b extractions survive filtering."""
    write_json(reports_dir / "aggregated_2026-09-14.json", {
        "date": "2026-09-14",
        "items": [
            {"title": "Climate stress test for insurers", "url": "https://example.org/stress"},
            {"title": "Solar energy report", "url": "https://example.org/solar"},
            {"title": "Annual actuarial congress", "url": "https://example.org/events/congress"},
        ],
    })
    write_json(reports_dir / "conferences_2026-09-14.json",
               {"conferences": [{"url": "https://example.org/events/congress"}]})
    result = run_script("step3b_generate_assessments.py", "--date", "2026-09-14",
                        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)})
    assert result.returncode == 0, result.stdout + result.stderr
    assessments = json.loads(
        (reports_dir / "hermes_assessments_2026-09-14.json").read_text())["assessments"]
    by_url = {a["url"]: a for a in assessments}
    assert by_url["https://example.org/stress"]["relevant"] is True
    assert by_url["https://example.org/solar"]["relevant"] is False
    assert by_url["https://example.org/events/congress"]["relevant"] is True
    assert by_url["https://example.org/events/congress"]["category"] == "conference"


def test_step5_help_marks_monitor_stats_required():
    """--help must match the fail-closed behaviour (it was labelled Optional)."""
    result = run_script("step5_build_md.py", "--help")
    assert result.returncode == 0
    assert "Required JSON" in result.stdout
