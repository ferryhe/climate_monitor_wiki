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


def test_step5_null_category_falls_back_to_general(reports_dir):
    """Legacy filtered_*.json with category:null (and a null inside
    categories) must render into the general section, not crash the join."""
    write_json(reports_dir / "filtered_2026-09-14.json", {
        "total_input": 1, "relevant": 1, "non_relevant": 0,
        "items": [{"title": "legacy item", "url": "https://example.org/legacy",
                   "category": None, "categories": [None, "scenario_analysis"],
                   "summary": "s", "keywords": [], "source": "OrgA",
                   "pillar": "A"}],
    })
    write_json(reports_dir / "hermes_assessments_2026-09-14.json", {
        "assessments": [], "executive_summary": "test summary"})
    write_json(reports_dir / "stats.json", {"checked": 57, "succeeded": 54, "failed": 3})
    env = {"CLIMATE_REPORTS_DIR": str(reports_dir)}
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        "--monitor-stats", str(reports_dir / "stats.json"),
                        "--allow-future", "--allow-offcycle", env_extra=env)
    assert result.returncode == 0, result.stdout + result.stderr
    md = (reports_dir / "climate-monitor-2026-09-14.md").read_text()
    assert "General" in md
    assert "Scenario Analysis" in md  # categories 里的合法元素仍展示
    sidecar = json.loads(
        (reports_dir / "climate-monitor-2026-09-14.json").read_text())
    items = sidecar["categories"]["general"]
    assert items[0]["categories"] == ["Scenario Analysis"]
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
    assert items[0]["categories"] == ["catastrophe_natcat"]


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
    assert by_url["https://example.org/events/congress"]["categories"] == ["conference"]


def test_step3b_fallback_multi_category_ordering(reports_dir):
    """Fallback assessments keep the full scored category list; the primary
    (highest-scoring) category is first and mirrors the 'category' field."""
    write_json(reports_dir / "aggregated_2026-09-14.json", {
        "date": "2026-09-14",
        "items": [
            {"title": "Parametric flood insurance scenario stress test",
             "url": "https://example.org/multi"},
        ],
    })
    result = run_script("step3b_generate_assessments.py", "--date", "2026-09-14",
                        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)})
    assert result.returncode == 0, result.stdout + result.stderr
    assessments = json.loads(
        (reports_dir / "hermes_assessments_2026-09-14.json").read_text())["assessments"]
    item = assessments[0]
    assert item["relevant"] is True
    assert len(item["categories"]) >= 2
    assert item["category"] == item["categories"][0]
    # scenario keywords ("scenario", "stress test") score higher than the
    # single flood/parametric hits in this synthetic title.
    assert item["categories"][0] == "scenario_analysis"


def test_step3_filter_preserves_ordered_multi_categories(reports_dir):
    """Assessments with 'categories' keep the validated, deduplicated order;
    the single 'category' field is derived from categories[0]. Invalid and
    duplicate entries are dropped."""
    write_json(reports_dir / "aggregated_2026-09-14.json", {
        "date": "2026-09-14",
        "items": [
            {"title": "t", "url": "https://example.org/m", "source": "OrgA", "pillar": "A"},
        ],
    })
    write_json(reports_dir / "hermes_assessments_2026-09-14.json", {
        "assessments": [
            {"id": 0, "url": "https://example.org/m", "relevant": True,
             "categories": ["financial_risk", "catastrophe_natcat", "financial_risk",
                            "not-a-category", "parametric_insurance"],
             "category": "financial_risk",
             "summary": "", "keywords": []},
        ],
    })
    result = run_script("step3_filter.py", "--date", "2026-09-14",
                        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)})
    assert result.returncode == 0, result.stdout + result.stderr
    items = json.loads((reports_dir / "filtered_2026-09-14.json").read_text())["items"]
    assert items[0]["categories"] == ["financial_risk", "catastrophe_natcat", "parametric_insurance"]
    assert items[0]["category"] == "financial_risk"


def test_step3_filter_legacy_single_category_still_works(reports_dir):
    """Legacy assessments without 'categories' derive the list from 'category'."""
    write_json(reports_dir / "aggregated_2026-09-14.json", {
        "date": "2026-09-14",
        "items": [
            {"title": "t", "url": "https://example.org/l", "source": "OrgA", "pillar": "A"},
        ],
    })
    write_json(reports_dir / "hermes_assessments_2026-09-14.json", {
        "assessments": [
            {"id": 0, "url": "https://example.org/l", "relevant": True,
             "category": "catastrophe_natcat", "summary": "", "keywords": []},
        ],
    })
    result = run_script("step3_filter.py", "--date", "2026-09-14",
                        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)})
    assert result.returncode == 0, result.stdout + result.stderr
    items = json.loads((reports_dir / "filtered_2026-09-14.json").read_text())["items"]
    assert items[0]["categories"] == ["catastrophe_natcat"]
    assert items[0]["category"] == "catastrophe_natcat"


def test_step5_emits_full_categories_and_primary_sections(reports_dir):
    """MD sections group by primary category; each item's Categories line and
    the JSON sidecar carry the full ordered list."""
    write_json(reports_dir / "filtered_2026-09-14.json", {
        "total_input": 1, "relevant": 1, "non_relevant": 0,
        "items": [{"title": "Flood stress test guidance", "url": "https://example.org/a",
                   "categories": ["catastrophe_natcat", "scenario_analysis", "financial_risk"],
                   "category": "catastrophe_natcat",
                   "summary": "", "keywords": ["flood"], "source": "OrgA", "pillar": "A"}],
    })
    write_json(reports_dir / "hermes_assessments_2026-09-14.json",
               {"assessments": [], "executive_summary": "test summary"})
    write_json(reports_dir / "stats.json", {"checked": 57, "succeeded": 54, "failed": 3})
    allow = ["--allow-future", "--allow-offcycle"]
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        "--monitor-stats", str(reports_dir / "stats.json"),
                        *allow, env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)})
    assert result.returncode == 0, result.stdout + result.stderr
    md = (reports_dir / "climate-monitor-2026-09-14.md").read_text()
    assert "### Catastrophe & NatCat (1)" in md
    assert "### Scenario Analysis (1)" not in md  # primary grouping only
    assert "**Categories:** Catastrophe & NatCat, Scenario Analysis, Financial Risk" in md
    sidecar = json.loads((reports_dir / "climate-monitor-2026-09-14.json").read_text())
    item = sidecar["categories"]["catastrophe_natcat"][0]
    assert item["categories"] == ["Catastrophe & NatCat", "Scenario Analysis", "Financial Risk"]


def test_step5_help_marks_monitor_stats_required():
    """--help must match the fail-closed behaviour (it was labelled Optional)."""
    result = run_script("step5_build_md.py", "--help")
    assert result.returncode == 0
    assert "Required JSON" in result.stdout


WEEKLY_MD = """# Weekly Climate Monitor

**Report Date:** 2026-09-14

## Executive Summary

Sites checked: **57**, succeeded: **54**, failed: **3**

## Pillar A

- **Flood insurance claims surge**
  https://example.org/flood
  - Grounded summary about flood claims.

## Pillar B

## Original Links

- https://example.org/flood
"""


def test_step8_promotion_is_pid_staged_and_rerun_safe(reports_dir, tmp_path):
    """step8 promotes the generated report into sources/ via a pid-unique
    staging file; a retried run must be a clean no-op without leftover
    .tmp.* files (fixed .tmp suffix once let concurrent runs clobber).
    The registry DB does NOT exist beforehand: step8 must bootstrap the
    schema itself (first-deploy path)."""
    import sqlite3

    sources = tmp_path / "sources"
    db_path = tmp_path / "registry.sqlite3"
    backup_dir = tmp_path / "backups"
    (reports_dir / "climate-monitor-2026-09-14.md").write_text(WEEKLY_MD)

    env = {
        "CLIMATE_REPORTS_DIR": str(reports_dir),
        "CLIMATE_WIKI_SOURCES": str(sources),
        "CLIMATE_REGISTRY_DB": str(db_path),
        "CLIMATE_REGISTRY_BACKUP_DIR": str(backup_dir),
    }
    result = run_script("step8_sync_registry.py", "--date", "2026-09-14",
                        "--allow-future", "--allow-offcycle", env_extra=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Initialized registry database" in result.stdout
    assert (sources / "climate-monitor-2026-09-14.md").read_text() == WEEKLY_MD
    assert not list(sources.glob("*.tmp*"))

    # Retried run: sources/ and generated agree -> no promotion, no conflict.
    result2 = run_script("step8_sync_registry.py", "--date", "2026-09-14",
                         "--allow-future", "--allow-offcycle", env_extra=env)
    assert result2.returncode == 0, result2.stdout + result2.stderr
    assert "Initialized registry database" not in result2.stdout
    assert "up to date" in result2.stdout
    assert not list(sources.glob("*.tmp*"))

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
    conn.close()


def test_cli_init_is_idempotent(tmp_path):
    """climate_registry init creates a migrated DB once and validates on rerun."""
    db_path = tmp_path / "registry.sqlite3"
    env = {"CLIMATE_WIKI_HOME": str(REPO)}
    first = subprocess.run(
        [PYTHON, "-m", "climate_registry", "init", "--database", str(db_path)],
        capture_output=True, text=True, env={**os.environ, **env}, cwd=REPO, timeout=60,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    payload = json.loads(first.stdout)
    assert payload["status"] == "ok" and payload["created"] is True

    second = subprocess.run(
        [PYTHON, "-m", "climate_registry", "init", "--database", str(db_path)],
        capture_output=True, text=True, env={**os.environ, **env}, cwd=REPO, timeout=60,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    payload2 = json.loads(second.stdout)
    assert payload2["status"] == "ok" and payload2["created"] is False
    assert payload2["schema_version"] == payload["schema_version"]

    import sqlite3
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == payload["schema_version"]
    assert conn.execute(
        "SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == payload["schema_version"]
    conn.close()
