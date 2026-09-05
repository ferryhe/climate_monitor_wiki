"""Focused contract tests for the new weekly pipeline scripts (scripts/step*.py).

These tests exercise the scripts as subprocesses against a temp data
directory (CLIMATE_REPORTS_DIR), so they stay independent of the production
/home/ubuntu paths.
"""
import hashlib
import json
import os
import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest

from scripts import step9_update_website
from scripts.publish_weekly_reports import validate_pending_reports

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


def article_changes(report_date, groups, *, generated_at=None):
    return {
        "date": report_date,
        "pillar": "A",
        "sites_with_changes": len(groups),
        "orgs_with_articles": len(groups),
        "baseline_urls": 0,
        "new_articles": sum(len(group["items"]) for group in groups),
        "seen_before": 0,
        "generated_at": generated_at or f"{report_date}T08:00:00Z",
        "articles": groups,
    }


def monitor_result(report_date, *, checked=57, succeeded=54, failed=3):
    failures = [
        {"source_id": f"source-{index}", "status": "failed", "error_code": "timeout"}
        for index in range(failed)
    ]
    return {
        "schema_version": "weekly-run-attempt.v1",
        "attempt_id": f"{report_date.replace('-', '')}t080000z-monitor-01",
        "stage": "monitor",
        "report_date": report_date,
        "scheduled_for": f"{report_date}T08:00:00Z",
        "finished_at": f"{report_date}T08:30:00Z",
        "status": "partial" if failed else "success",
        "result_code": "report_written_with_failures" if failed else "report_written",
        "sources": {
            "total": checked,
            "updated": succeeded,
            "unchanged": 0,
            "failed": failed,
            "blocked": 0,
            "failures": failures,
        },
    }


def bind_monitor_result_to_report(path, result):
    sources = result["sources"]
    unknown = (
        b"Sites checked: **unknown**, succeeded: **unknown**, failed: **unknown**"
    )
    evidenced = (
        f"Sites checked: **{sources['total']}**, succeeded: "
        f"**{sources['updated'] + sources['unchanged']}**, failed: "
        f"**{sources['failed'] + sources['blocked']}**"
    ).encode("ascii")
    candidate = path.read_bytes()
    assert candidate.count(unknown) == 1
    result["report"] = {
        "report_id": path.stem,
        "report_date": result["report_date"],
        "sha256": hashlib.sha256(candidate.replace(unknown, evidenced)).hexdigest(),
    }
    return result


def test_step3_rejects_71_change_event_rows_with_grouped_reasons(reports_dir):
    report_date = "2026-08-31"
    write_json(reports_dir / f"article_changes_{report_date}.json", {
        "date": report_date,
        "pillar": "A",
        "articles": [{
            "org": "Example Org",
            "items": [
                {
                    "id": index,
                    "change_type": "new_content",
                    "detected_at": "2026-08-31T08:00:00Z",
                    "summary": "Content changed on Example Org",
                    "diff_snippet": "https://example.org/article",
                }
                for index in range(71)
            ],
        }],
    })
    write_json(reports_dir / f"pillar_b_{report_date}.json", [{
        "title": "Climate risk for insurers",
        "url": "https://example.org/climate-risk",
        "source": "web",
        "summary": "Evidence from the search result.",
    }])

    result = run_script(
        "step3_aggregate.py", "--date", report_date,
        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)},
    )

    assert result.returncode == 1
    assert "71 malformed article rows" in result.stdout
    assert "article_changes_2026-08-31.json: 71" in result.stdout
    assert "missing_title=71" in result.stdout
    assert "missing_url=71" in result.stdout
    assert "unexpected_fields=71" in result.stdout
    assert not (reports_dir / f"aggregated_{report_date}.json").exists()


def test_step3_invalidates_stale_aggregate_before_rejecting_malformed_rows(
    reports_dir,
):
    report_date = "2026-08-31"
    aggregate = reports_dir / f"aggregated_{report_date}.json"
    aggregate.write_text('{"items":[{"title":"stale"}]}')
    write_json(reports_dir / f"article_changes_{report_date}.json", {
        "date": report_date,
        "pillar": "A",
        "articles": [{
            "org": "Example Org",
            "items": [{
                "change_type": "new_content",
                "detected_at": "2026-08-31T08:00:00Z",
                "diff_snippet": "https://example.org/article",
            }],
        }],
    })

    result = run_script(
        "step3_aggregate.py", "--date", report_date,
        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)},
    )

    assert result.returncode == 1
    assert "1 malformed article rows" in result.stdout
    assert not aggregate.exists()


def test_step3_invalidates_stale_combined_evidence_before_rejecting_rows(reports_dir):
    report_date = "2026-08-31"
    combined = reports_dir / f"combined-candidates_{report_date}.json"
    state = reports_dir / "article_state.json"
    state.write_bytes(b'{"Example Org":["https://example.org/existing"]}\r\n')
    state_before = state.read_bytes()
    combined.write_text('{"items":[{"title":"stale"}]}')
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        {
            "date": report_date,
            "pillar": "A",
            "articles": [
                {
                    "org": "Example Org",
                    "items": [{"change_type": "new_content"}],
                }
            ],
        },
    )

    result = run_script(
        "step3_aggregate.py",
        "--date",
        report_date,
        env_extra={
            "CLIMATE_REPORTS_DIR": str(reports_dir),
            "CLIMATE_WL_STATE": str(state),
        },
    )

    assert result.returncode == 1
    assert "malformed article rows" in result.stdout
    assert not combined.exists()
    assert state.read_bytes() == state_before


def test_step3_groups_missing_pillar_a_categories_and_invalidates_stale_outputs(
    reports_dir,
):
    report_date = "2026-08-31"
    aggregate = reports_dir / f"aggregated_{report_date}.json"
    combined = reports_dir / f"combined-candidates_{report_date}.json"
    aggregate.write_bytes(b"stale aggregate\n")
    combined.write_bytes(b"stale combined\n")
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        article_changes(
            report_date,
            [{
                "org": "Example Org",
                "items": [{
                    "title": "Climate insurance update",
                    "url": "https://example.org/update",
                }],
            }],
        ),
    )
    write_json(reports_dir / f"pillar_b_{report_date}.json", [])

    result = run_script(
        "step3_aggregate.py",
        "--date",
        report_date,
        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)},
    )

    assert result.returncode == 1
    assert "1 malformed article rows" in result.stdout
    assert "invalid_categories=1" in result.stdout
    assert not aggregate.exists()
    assert not combined.exists()


def test_step3_reports_failure_to_invalidate_aggregate(reports_dir):
    report_date = "2026-08-31"
    output = reports_dir / "occupied-output"
    output.mkdir()

    result = run_script(
        "step3_aggregate.py", "--date", report_date, "--output", str(output),
        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)},
    )

    assert result.returncode == 1
    assert "cannot invalidate stale aggregate" in result.stdout


def test_step3_never_invalidates_an_input_artifact(reports_dir):
    report_date = "2026-08-31"
    pillar_a = reports_dir / f"article_changes_{report_date}.json"
    write_json(pillar_a, {
        "date": report_date,
        "pillar": "A",
        "articles": [],
    })
    before = pillar_a.read_bytes()

    result = run_script(
        "step3_aggregate.py", "--date", report_date,
        "--output", str(pillar_a),
        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)},
    )

    assert result.returncode == 1
    assert "must not overwrite an input artifact" in result.stdout
    assert pillar_a.read_bytes() == before


def test_step3_validates_both_article_schemas_and_records_provenance(reports_dir):
    report_date = "2026-09-14"
    pillar_a_url = "https://example.org/articles/climate-risk?edition=exact"
    pillar_b_url = "https://example.net/research/insurance-transition#findings"
    write_json(reports_dir / f"article_changes_{report_date}.json", article_changes(
        report_date,
        [{
            "org": "Example Org",
            "items": [{
                "title": "Climate risk guidance",
                "url": pillar_a_url,
                "categories": ["financial_risk"],
            }],
        }],
    ))
    write_json(reports_dir / f"pillar_b_{report_date}.json", [{
        "title": "Insurance transition research",
        "url": pillar_b_url,
        "source": "web",
        "summary": "Evidence from the search result.",
    }])

    result = run_script(
        "step3_aggregate.py", "--date", report_date,
        env_extra={
            "CLIMATE_REPORTS_DIR": str(reports_dir),
            "CLIMATE_WL_STATE": str(reports_dir / "article_state.json"),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    items = json.loads(
        (reports_dir / f"aggregated_{report_date}.json").read_text()
    )["items"]
    assert [item["url"] for item in items] == [pillar_b_url, pillar_a_url]
    assert [item["pillar"] for item in items] == ["B", "A"]
    assert [item["provenance"] for item in items] == [
        {
            "artifact": f"pillar_b_{report_date}.json",
            "record": "[0]",
        },
        {
            "artifact": f"article_changes_{report_date}.json",
            "record": "articles[0].items[0]",
        },
    ]
    write_json(reports_dir / f"hermes_assessments_{report_date}.json", {
        "assessments": [
            {"id": 0, "url": pillar_a_url, "relevant": True, "category": "financial_risk"},
            {"id": 1, "url": pillar_b_url, "relevant": True, "category": "financial_risk"},
        ],
    })
    filtered_result = run_script(
        "step3_filter.py", "--date", report_date,
        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)},
    )
    assert filtered_result.returncode == 0, filtered_result.stdout + filtered_result.stderr
    filtered_items = json.loads(
        (reports_dir / f"filtered_{report_date}.json").read_text()
    )["items"]
    assert [item["provenance"] for item in filtered_items] == [
        item["provenance"] for item in items
    ]


@pytest.mark.parametrize(
    ("pillar_a_item", "pillar_b_item", "reason"),
    [
        (
            {
                "title": "Climate report",
                "url": "https://example.org/",
                "categories": ["general"],
            },
            {"title": "Insurance report", "url": "https://example.net/story", "source": "web", "summary": ""},
            "publication_ineligible_url=1",
        ),
        (
            {
                "title": "Climate report",
                "url": "https://example.org/story",
                "categories": ["general"],
            },
            {"title": "Insurance report", "url": "https://example.net/story", "source": "wire", "summary": ""},
            "invalid_source=1",
        ),
        (
            {"title": "Climate report", "url": "https://example.org/story", "categories": "general"},
            {"title": "Insurance report", "url": "https://example.net/story", "source": "web", "summary": ""},
            "invalid_categories=1",
        ),
        (
            {
                "title": "Climate report",
                "url": "https://example.org/story",
                "categories": ["general"],
                "summary": "not an upstream field",
            },
            {"title": "Insurance report", "url": "https://example.net/story", "source": "web", "summary": ""},
            "unexpected_fields=1",
        ),
        (
            {"title": "Climate report", "url": "https://example.org/story", "categories": []},
            {"title": "Insurance report", "url": "https://example.net/story", "source": "web", "summary": ""},
            "invalid_categories=1",
        ),
    ],
)
def test_step3_fails_closed_when_either_article_schema_is_invalid(
    reports_dir, pillar_a_item, pillar_b_item, reason
):
    report_date = "2026-09-14"
    write_json(reports_dir / f"article_changes_{report_date}.json", {
        "date": report_date,
        "pillar": "A",
        "articles": [{"org": "Example Org", "items": [pillar_a_item]}],
    })
    write_json(reports_dir / f"pillar_b_{report_date}.json", [pillar_b_item])

    result = run_script(
        "step3_aggregate.py", "--date", report_date,
        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)},
    )

    assert result.returncode == 1
    assert "1 malformed article rows" in result.stdout
    assert reason in result.stdout
    assert not (reports_dir / f"aggregated_{report_date}.json").exists()


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


def test_step5_accepts_only_valid_same_report_monitor_results(reports_dir):
    write_json(reports_dir / "filtered_2026-09-14.json", {
        "total_input": 1, "relevant": 1, "non_relevant": 0,
        "items": [{"title": "t", "url": "https://example.org/a", "category": "general",
                   "summary": "", "keywords": [], "source": "OrgA", "pillar": "A"}],
    })
    write_json(reports_dir / "hermes_assessments_2026-09-14.json", {
        "assessments": [], "executive_summary": "test summary"})
    env = {"CLIMATE_REPORTS_DIR": str(reports_dir)}
    allow = ["--allow-future", "--allow-offcycle"]

    # A legacy count-only document is not an evidenced monitor result.
    write_json(reports_dir / "monitor-result.json", {
        "checked": 57, "succeeded": 54, "failed": 3,
    })
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        "--monitor-result", str(reports_dir / "monitor-result.json"),
                        *allow, env_extra=env)
    assert result.returncode == 1
    assert "validated weekly-run-attempt.v1" in result.stdout

    # A valid result from a different report identity must fail.
    write_json(reports_dir / "monitor-result.json", monitor_result("2026-09-21"))
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        "--monitor-result", str(reports_dir / "monitor-result.json"),
                        *allow, env_extra=env)
    assert result.returncode == 1
    assert "report_date does not match" in result.stdout

    # A non-canonical report identity cannot bind totals to this candidate.
    result_with_wrong_report = monitor_result("2026-09-14")
    result_with_wrong_report["report"] = {
        "report_id": "different-report",
        "report_date": "2026-09-14",
        "sha256": "a" * 64,
    }
    write_json(reports_dir / "monitor-result.json", result_with_wrong_report)
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        "--monitor-result", str(reports_dir / "monitor-result.json"),
                        *allow, env_extra=env)
    assert result.returncode == 0, result.stdout + result.stderr
    md = (reports_dir / "climate-monitor-2026-09-14.md").read_text()
    assert "Sites checked: **unknown**" in md

    # A valid result without an exact report identity also remains unknown.
    write_json(reports_dir / "monitor-result.json", monitor_result("2026-09-14"))
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        "--monitor-result", str(reports_dir / "monitor-result.json"),
                        *allow, env_extra=env)
    assert result.returncode == 0, result.stdout + result.stderr
    report = reports_dir / "climate-monitor-2026-09-14.md"
    assert "Sites checked: **unknown**" in report.read_text()

    # Only an exact raw-byte report identity supplies acquisition totals.
    bound_result = bind_monitor_result_to_report(
        report, monitor_result("2026-09-14")
    )
    write_json(reports_dir / "monitor-result.json", bound_result)
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        "--monitor-result", str(reports_dir / "monitor-result.json"),
                        *allow, env_extra=env)
    assert result.returncode == 0, result.stdout + result.stderr
    md = report.read_text()
    assert "Sites checked: **57**, succeeded: **54**, failed: **3**" in md


def test_step5_missing_monitor_result_is_unknown_and_passes_registry_gate(
    reports_dir, tmp_path
):
    report_date = "2026-09-14"
    write_json(reports_dir / f"filtered_{report_date}.json", {
        "total_input": 1,
        "relevant": 1,
        "non_relevant": 0,
        "items": [{
            "title": "Climate risk for insurers",
            "url": "https://example.org/reports/climate-risk",
            "category": "financial_risk",
            "summary": "",
            "keywords": [],
            "source": "Example Org",
            "pillar": "A",
            "provenance": {
                "artifact": f"article_changes_{report_date}.json",
                "record": "articles[0].items[0]",
            },
        }],
    })
    write_json(reports_dir / f"hermes_assessments_{report_date}.json", {
        "assessments": [], "executive_summary": "Evidence-safe summary",
    })

    result = run_script(
        "step5_build_md.py", "--date", report_date,
        "--allow-future", "--allow-offcycle",
        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = reports_dir / f"climate-monitor-{report_date}.md"
    md = report.read_text()
    assert "Sites checked: **unknown**, succeeded: **unknown**, failed: **unknown**" in md
    assert "Sites checked: **0**" not in md
    assert "Sites checked: **57**" not in md
    sidecar = json.loads(report.with_suffix(".json").read_text())
    assert sidecar["categories"]["financial_risk"][0]["provenance"] == {
        "artifact": f"article_changes_{report_date}.json",
        "record": "articles[0].items[0]",
    }
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    assert validate_pending_reports(
        [report], source_dir=source_dir, allow_offcycle=True
    ) == {report: hashlib.sha256(report.read_bytes()).hexdigest()}


def test_step5_same_date_sibling_report_sha_does_not_supply_totals(reports_dir):
    report_date = "2026-09-14"
    write_json(reports_dir / f"filtered_{report_date}.json", {
        "total_input": 1,
        "relevant": 1,
        "non_relevant": 0,
        "items": [{
            "title": "Climate risk for insurers",
            "url": "https://example.org/reports/climate-risk",
            "category": "financial_risk",
            "summary": "",
            "keywords": [],
            "source": "Example Org",
            "pillar": "A",
        }],
    })
    sibling = monitor_result(report_date)
    sibling["attempt_id"] = "20260914t081500z-monitor-sibling"
    sibling["report"] = {
        "report_id": f"climate-monitor-{report_date}",
        "report_date": report_date,
        "sha256": "a" * 64,
    }
    write_json(reports_dir / "sibling-monitor-result.json", sibling)

    result = run_script(
        "step5_build_md.py", "--date", report_date,
        "--monitor-result", str(reports_dir / "sibling-monitor-result.json"),
        "--allow-future", "--allow-offcycle",
        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    md = (reports_dir / f"climate-monitor-{report_date}.md").read_text()
    assert "Sites checked: **unknown**, succeeded: **unknown**, failed: **unknown**" in md
    assert "Sites checked: **57**" not in md


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
    env = {"CLIMATE_REPORTS_DIR": str(reports_dir)}
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        "--allow-future", "--allow-offcycle", env_extra=env)
    assert result.returncode == 0, result.stdout + result.stderr
    md = (reports_dir / "climate-monitor-2026-09-14.md").read_text()
    assert "General" in md
    assert "Scenario Analysis" in md  # valid entries inside categories still render
    sidecar = json.loads(
        (reports_dir / "climate-monitor-2026-09-14.json").read_text())
    items = sidecar["categories"]["general"]
    assert items[0]["categories"] == ["Scenario Analysis"]
    md = (reports_dir / "climate-monitor-2026-09-14.md").read_text()
    assert "Sites checked: **unknown**, succeeded: **unknown**, failed: **unknown**" in md
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


def test_step2_save_state_defers_legacy_state_until_explicit_post_report_commit(reports_dir):
    state_dir = reports_dir / "state"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "article_state.json"
    write_json(state_file, {"__pillar_b__": ["https://example.org/b1"]})
    before = state_file.read_bytes()
    result = run_script("step2_save_state.py", "--date", "2026-09-14",
                        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir),
                                   "CLIMATE_WL_STATE": str(state_file)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DEFERRED" in result.stdout
    assert state_file.read_bytes() == before


def test_legacy_pending_url_delta_commits_only_after_complete_report_bundle(reports_dir):
    report_date = "2026-09-14"
    state_file = reports_dir / "state" / "article_state.json"
    state_file.parent.mkdir()
    write_json(state_file, {"__pillar_b__": ["https://example.org/existing"]})
    before = state_file.read_bytes()
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        article_changes(report_date, []),
    )
    write_json(
        reports_dir / f"pillar_b_{report_date}.json",
        [{
            "title": "Climate insurance evidence",
            "url": "https://example.org/new?utm_source=mail#part",
            "source": "web",
            "summary": "Search-result evidence.",
        }],
    )
    env = {
        "CLIMATE_REPORTS_DIR": str(reports_dir),
        "CLIMATE_WL_STATE": str(state_file),
    }

    aggregate = run_script("step3_aggregate.py", "--date", report_date, env_extra=env)
    assert aggregate.returncode == 0, aggregate.stdout + aggregate.stderr
    assert state_file.read_bytes() == before
    pending = state_file.with_name(state_file.name + ".pending-urls.json")
    assert pending.exists()

    early = run_script(
        "step2_save_state.py", "--date", report_date, "--commit-pending", env_extra=env
    )
    assert early.returncode == 1
    assert state_file.read_bytes() == before

    report = reports_dir / f"climate-monitor-{report_date}.md"
    report.write_text(
        "# final report\n\n"
        f"**Report Date:** {report_date}\n\n"
        "- https://example.org/new?utm_source=mail\n",
        encoding="utf-8",
    )
    write_json(
        report.with_suffix(".json"),
        {
            "report_date": report_date,
            "total_input": 1,
            "relevant": 1,
            "non_relevant": 0,
            "categories": {
                "general": [{"url": "https://example.org/new?utm_source=mail"}]
            },
        },
    )
    pending_before_dry_run = pending.read_bytes()
    dry_run = run_script(
        "step2_save_state.py",
        "--date",
        report_date,
        "--commit-pending",
        "--dry-run",
        env_extra=env,
    )
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    assert state_file.read_bytes() == before
    assert pending.read_bytes() == pending_before_dry_run
    committed = run_script(
        "step2_save_state.py", "--date", report_date, "--commit-pending", env_extra=env
    )
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert json.loads(state_file.read_text(encoding="utf-8"))["__pillar_b__"] == [
        "https://example.org/existing",
        "https://example.org/new",
    ]
    assert not pending.exists()


def test_legacy_commit_rejects_a_pending_delta_from_a_different_aggregate(reports_dir):
    report_date = "2026-09-14"
    state_file = reports_dir / "state" / "article_state.json"
    state_file.parent.mkdir()
    state_file.write_text("{}\n", encoding="utf-8")
    before = state_file.read_bytes()
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        article_changes(report_date, []),
    )
    pillar_b = reports_dir / f"pillar_b_{report_date}.json"
    env = {
        "CLIMATE_REPORTS_DIR": str(reports_dir),
        "CLIMATE_WL_STATE": str(state_file),
    }

    write_json(pillar_b, [{
        "title": "Old candidate",
        "url": "https://example.org/old",
        "source": "web",
        "summary": "Old climate evidence.",
    }])
    first = run_script("step3_aggregate.py", "--date", report_date, env_extra=env)
    assert first.returncode == 0, first.stdout + first.stderr

    write_json(pillar_b, [{
        "title": "New candidate",
        "url": "https://example.org/new",
        "source": "web",
        "summary": "New climate evidence.",
    }])
    second = run_script(
        "step3_aggregate.py",
        "--date",
        report_date,
        "--no-update-seen-state",
        "--state-file",
        str(reports_dir / "isolated-read-only-state.json"),
        env_extra=env,
    )
    assert second.returncode == 0, second.stdout + second.stderr

    report = reports_dir / f"climate-monitor-{report_date}.md"
    report.write_text(
        "# final report\n\n"
        f"**Report Date:** {report_date}\n\n"
        "- https://example.org/new\n",
        encoding="utf-8",
    )
    write_json(
        report.with_suffix(".json"),
        {
            "report_date": report_date,
            "total_input": 1,
            "relevant": 1,
            "non_relevant": 0,
            "categories": {"general": [{"url": "https://example.org/new"}]},
        },
    )

    result = run_script(
        "step2_save_state.py", "--date", report_date, "--commit-pending", env_extra=env
    )

    assert result.returncode == 1
    assert "different combined candidates" in result.stdout
    assert state_file.read_bytes() == before


def _run_legacy_report_cycle(reports_dir, state_file, report_date):
    environment = {
        "CLIMATE_REPORTS_DIR": str(reports_dir),
        "CLIMATE_WL_STATE": str(state_file),
    }
    for script, arguments in (
        ("step3_aggregate.py", ("--date", report_date)),
        ("step3_filter.py", ("--date", report_date)),
        ("step5_build_md.py", ("--date", report_date)),
        (
            "step2_save_state.py",
            ("--date", report_date, "--commit-pending"),
        ),
    ):
        result = run_script(script, *arguments, env_extra=environment)
        assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_same_day_successful_replay_keeps_bundle_and_state_stable(reports_dir):
    report_date = "2026-08-31"
    state_file = reports_dir / "state" / "article_state.json"
    state_file.parent.mkdir()
    state_file.write_bytes(b"{}\n")
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        article_changes(report_date, []),
    )
    write_json(
        reports_dir / f"pillar_b_{report_date}.json",
        [
            {
                "title": "Climate insurance replay report",
                "url": "https://example.org/replay?utm_source=mail#findings",
                "source": "web",
                "summary": "Search-result evidence.",
            }
        ],
    )

    _run_legacy_report_cycle(reports_dir, state_file, report_date)
    paths = {
        "aggregate": reports_dir / f"aggregated_{report_date}.json",
        "combined": reports_dir / f"combined-candidates_{report_date}.json",
        "report": reports_dir / f"climate-monitor-{report_date}.md",
        "evidence": reports_dir / f"climate-monitor-{report_date}.json",
        "state": state_file,
    }
    first_bytes = {name: path.read_bytes() for name, path in paths.items()}

    _run_legacy_report_cycle(reports_dir, state_file, report_date)

    assert {name: path.read_bytes() for name, path in paths.items()} == first_bytes
    combined = json.loads(paths["combined"].read_text(encoding="utf-8"))
    assert combined["counts"]["history_skips"] == 0
    assert [item["canonical_url"] for item in combined["items"]] == [
        "https://example.org/replay"
    ]
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "__pillar_b__": ["https://example.org/replay"]
    }


def test_legacy_same_day_replay_keeps_old_and_adds_only_new_url_state(reports_dir):
    report_date = "2026-08-31"
    state_file = reports_dir / "state" / "article_state.json"
    state_file.parent.mkdir()
    state_file.write_bytes(b"{}\n")
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        article_changes(report_date, []),
    )
    pillar_b = reports_dir / f"pillar_b_{report_date}.json"
    old = {
        "title": "Climate insurance old report",
        "url": "https://example.org/old",
        "source": "web",
        "summary": "Old search-result evidence.",
    }
    new = {
        "title": "Climate insurance new report",
        "url": "https://example.org/new",
        "source": "web",
        "summary": "New search-result evidence.",
    }
    write_json(pillar_b, [old])
    _run_legacy_report_cycle(reports_dir, state_file, report_date)
    first_state = state_file.read_bytes()

    write_json(pillar_b, [old, new])
    _run_legacy_report_cycle(reports_dir, state_file, report_date)

    assert json.loads(first_state) == {"__pillar_b__": ["https://example.org/old"]}
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "__pillar_b__": ["https://example.org/old", "https://example.org/new"]
    }
    combined = json.loads(
        (reports_dir / f"combined-candidates_{report_date}.json").read_text(
            encoding="utf-8"
        )
    )
    assert combined["counts"]["unique_urls"] == 2
    assert combined["counts"]["history_skips"] == 0
    assert {item["canonical_url"] for item in combined["items"]} == {
        "https://example.org/old",
        "https://example.org/new",
    }
    report = (reports_dir / f"climate-monitor-{report_date}.md").read_text(
        encoding="utf-8"
    )
    assert report.count("https://example.org/old") == 2
    assert report.count("https://example.org/new") == 2


def test_legacy_same_day_incremental_input_carries_committed_candidates_forward(
    reports_dir,
):
    report_date = "2026-08-31"
    state_file = reports_dir / "state" / "article_state.json"
    state_file.parent.mkdir()
    state_file.write_bytes(b"{}\n")
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        article_changes(report_date, []),
    )
    pillar_b = reports_dir / f"pillar_b_{report_date}.json"
    old = {
        "title": "Climate insurance old report",
        "url": "https://example.org/old",
        "source": "web",
        "summary": "Old search-result evidence.",
    }
    new = {
        "title": "Climate insurance new report",
        "url": "https://example.org/new",
        "source": "web",
        "summary": "New search-result evidence.",
    }
    write_json(pillar_b, [old])
    _run_legacy_report_cycle(reports_dir, state_file, report_date)
    first_combined = json.loads(
        (reports_dir / f"combined-candidates_{report_date}.json").read_text(
            encoding="utf-8"
        )
    )

    write_json(pillar_b, [new])
    _run_legacy_report_cycle(reports_dir, state_file, report_date)

    paths = {
        "aggregate": reports_dir / f"aggregated_{report_date}.json",
        "combined": reports_dir / f"combined-candidates_{report_date}.json",
        "report": reports_dir / f"climate-monitor-{report_date}.md",
        "evidence": reports_dir / f"climate-monitor-{report_date}.json",
        "state": state_file,
    }
    combined = json.loads(paths["combined"].read_text(encoding="utf-8"))
    assert combined["counts"] == {
        "pillar_a_rows": 0,
        "pillar_b_rows": 2,
        "unique_urls": 2,
        "cross_pillar_merges": 0,
        "history_skips": 0,
        "invalid_rows": 0,
    }
    assert [item["canonical_url"] for item in combined["items"]] == [
        "https://example.org/new",
        "https://example.org/old",
    ]
    assert all(len(item["origins"]) == 1 for item in combined["items"])
    carried = next(
        item
        for item in combined["items"]
        if item["canonical_url"] == "https://example.org/old"
    )
    assert carried["origins"] == first_combined["items"][0]["origins"]
    report = paths["report"].read_text(encoding="utf-8")
    assert report.count("https://example.org/old") == 2
    assert report.count("https://example.org/new") == 2
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "__pillar_b__": ["https://example.org/old", "https://example.org/new"]
    }
    incremental_bytes = {name: path.read_bytes() for name, path in paths.items()}

    _run_legacy_report_cycle(reports_dir, state_file, report_date)

    assert {name: path.read_bytes() for name, path in paths.items()} == incremental_bytes


def test_legacy_step3_stages_next_combined_until_report_bundle_commit(reports_dir):
    from scripts.step2_save_state import validate_final_report_bundle

    report_date = "2026-08-31"
    state_file = reports_dir / "state" / "article_state.json"
    state_file.parent.mkdir()
    state_file.write_bytes(b"{}\n")
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        article_changes(report_date, []),
    )
    pillar_b = reports_dir / f"pillar_b_{report_date}.json"
    old = {
        "title": "Climate insurance old report",
        "url": "https://example.org/old",
        "source": "web",
        "summary": "Old search-result evidence.",
    }
    new = {
        "title": "Climate insurance new report",
        "url": "https://example.org/new",
        "source": "web",
        "summary": "New search-result evidence.",
    }
    write_json(pillar_b, [old])
    _run_legacy_report_cycle(reports_dir, state_file, report_date)
    environment = {
        "CLIMATE_REPORTS_DIR": str(reports_dir),
        "CLIMATE_WL_STATE": str(state_file),
    }
    report = reports_dir / f"climate-monitor-{report_date}.md"
    evidence = report.with_suffix(".json")
    combined = reports_dir / f"combined-candidates_{report_date}.json"
    protected = (
        report.read_bytes(),
        evidence.read_bytes(),
        combined.read_bytes(),
    )
    write_json(pillar_b, [old, new])

    aggregate = run_script(
        "step3_aggregate.py", "--date", report_date, env_extra=environment
    )

    assert aggregate.returncode == 0, aggregate.stdout + aggregate.stderr
    assert (
        report.read_bytes(),
        evidence.read_bytes(),
        combined.read_bytes(),
    ) == protected
    assert validate_final_report_bundle(
        report=report,
        evidence=evidence,
        combined=combined,
        report_date=report_date,
    ) == {"https://example.org/old"}
    staged_combined = combined.with_name(combined.name + ".next")
    staged = json.loads(staged_combined.read_text(encoding="utf-8"))
    assert {item["canonical_url"] for item in staged["items"]} == {
        "https://example.org/old",
        "https://example.org/new",
    }

    for script, arguments in (
        ("step3b_generate_assessments.py", ("--date", report_date, "--force")),
        ("step3_filter.py", ("--date", report_date)),
        ("step5_build_md.py", ("--date", report_date)),
        ("step2_save_state.py", ("--date", report_date, "--commit-pending")),
    ):
        result = run_script(script, *arguments, env_extra=environment)
        assert result.returncode == 0, result.stdout + result.stderr

    assert not staged_combined.exists()
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "__pillar_b__": ["https://example.org/old", "https://example.org/new"]
    }
    assert validate_final_report_bundle(
        report=report,
        evidence=evidence,
        combined=combined,
        report_date=report_date,
    ) == {"https://example.org/old", "https://example.org/new"}


def test_legacy_custom_combined_path_completes_initial_and_incremental_cycles(
    reports_dir,
):
    from scripts.step2_save_state import validate_final_report_bundle

    report_date = "2026-08-31"
    state_file = reports_dir / "state" / "article_state.json"
    state_file.parent.mkdir()
    state_file.write_bytes(b"{}\n")
    custom_combined = reports_dir / "evidence" / "custom-candidates.json"
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        article_changes(report_date, []),
    )
    pillar_b = reports_dir / f"pillar_b_{report_date}.json"
    old = {
        "title": "Climate insurance old report",
        "url": "https://example.org/old",
        "source": "web",
        "summary": "Old search-result evidence.",
    }
    new = {
        "title": "Climate insurance new report",
        "url": "https://example.org/new",
        "source": "web",
        "summary": "New search-result evidence.",
    }
    environment = {
        "CLIMATE_REPORTS_DIR": str(reports_dir),
        "CLIMATE_WL_STATE": str(state_file),
    }

    def run_cycle():
        for script, arguments in (
            (
                "step3_aggregate.py",
                ("--date", report_date, "--combined-output", str(custom_combined)),
            ),
            ("step3_filter.py", ("--date", report_date)),
            (
                "step5_build_md.py",
                ("--date", report_date, "--combined", str(custom_combined)),
            ),
            (
                "step2_save_state.py",
                (
                    "--date",
                    report_date,
                    "--combined",
                    str(custom_combined),
                    "--commit-pending",
                ),
            ),
        ):
            result = run_script(script, *arguments, env_extra=environment)
            assert result.returncode == 0, result.stdout + result.stderr

    write_json(pillar_b, [old])
    run_cycle()
    report = reports_dir / f"climate-monitor-{report_date}.md"
    evidence = report.with_suffix(".json")
    assert validate_final_report_bundle(
        report=report,
        evidence=evidence,
        combined=custom_combined,
        report_date=report_date,
    ) == {"https://example.org/old"}
    assert not (reports_dir / f"combined-candidates_{report_date}.json").exists()

    write_json(pillar_b, [new])
    run_cycle()

    assert not custom_combined.with_name(custom_combined.name + ".next").exists()
    assert validate_final_report_bundle(
        report=report,
        evidence=evidence,
        combined=custom_combined,
        report_date=report_date,
    ) == {"https://example.org/old", "https://example.org/new"}
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "__pillar_b__": ["https://example.org/old", "https://example.org/new"]
    }


def test_legacy_report_bundle_recovers_interruption_between_artifact_promotions(
    reports_dir, monkeypatch
):
    from scripts import step5_build_md
    from scripts.step2_save_state import validate_final_report_bundle

    report_date = "2026-08-31"
    state_file = reports_dir / "state" / "article_state.json"
    state_file.parent.mkdir()
    state_file.write_bytes(b"{}\n")
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        article_changes(report_date, []),
    )
    write_json(
        reports_dir / f"pillar_b_{report_date}.json",
        [{
            "title": "Climate insurance recovery report",
            "url": "https://example.org/recovery",
            "source": "web",
            "summary": "Recovery search-result evidence.",
        }],
    )
    _run_legacy_report_cycle(reports_dir, state_file, report_date)
    report = reports_dir / f"climate-monitor-{report_date}.md"
    evidence = report.with_suffix(".json")
    combined = reports_dir / f"combined-candidates_{report_date}.json"
    payloads = (report.read_bytes(), evidence.read_bytes(), combined.read_bytes())
    real_replace = step5_build_md.os.replace
    promotions = 0

    def interrupted_replace(source, destination):
        nonlocal promotions
        if str(source).endswith(".legacy.pending"):
            promotions += 1
            if promotions == 2:
                raise KeyboardInterrupt("simulated legacy bundle promotion interruption")
        return real_replace(source, destination)

    monkeypatch.setattr(step5_build_md.os, "replace", interrupted_replace)
    with pytest.raises(KeyboardInterrupt, match="promotion interruption"):
        step5_build_md.commit_legacy_report_bundle(
            report_path=report,
            report_bytes=payloads[0],
            evidence_path=evidence,
            evidence_bytes=payloads[1],
            combined_path=combined,
            combined_bytes=payloads[2],
            report_date=report_date,
        )

    monkeypatch.setattr(step5_build_md.os, "replace", real_replace)
    assert step5_build_md.recover_legacy_report_bundle(
        report_path=report,
        evidence_path=evidence,
        combined_path=combined,
        report_date=report_date,
    ) == "applied"
    assert validate_final_report_bundle(
        report=report,
        evidence=evidence,
        combined=combined,
        report_date=report_date,
    ) == {"https://example.org/recovery"}
    assert not list(reports_dir.glob("*.legacy.pending"))
    assert not list(reports_dir.glob("*.legacy-bundle.pending.json"))


def test_legacy_different_date_does_not_overwrite_bound_pending_delta(reports_dir):
    first_date = "2026-08-24"
    second_date = "2026-08-31"
    state_file = reports_dir / "state" / "article_state.json"
    state_file.parent.mkdir()
    state_file.write_bytes(b"{}\n")
    for report_date, suffix in ((first_date, "first"), (second_date, "second")):
        write_json(
            reports_dir / f"article_changes_{report_date}.json",
            article_changes(report_date, []),
        )
        write_json(
            reports_dir / f"pillar_b_{report_date}.json",
            [
                {
                    "title": f"Climate insurance {suffix} report",
                    "url": f"https://example.org/{suffix}",
                    "source": "web",
                    "summary": f"{suffix.title()} search-result evidence.",
                }
            ],
        )
    environment = {
        "CLIMATE_REPORTS_DIR": str(reports_dir),
        "CLIMATE_WL_STATE": str(state_file),
    }
    for script in ("step3_aggregate.py", "step3_filter.py", "step5_build_md.py"):
        result = run_script(script, "--date", first_date, env_extra=environment)
        assert result.returncode == 0, result.stdout + result.stderr

    pending = state_file.with_name(state_file.name + ".pending-urls.json")
    pending_before = pending.read_bytes()
    read_only = run_script(
        "step3_aggregate.py",
        "--date",
        second_date,
        "--no-update-seen-state",
        env_extra=environment,
    )
    assert read_only.returncode == 0, read_only.stdout + read_only.stderr
    second_aggregate = reports_dir / f"aggregated_{second_date}.json"
    second_combined = reports_dir / f"combined-candidates_{second_date}.json"
    read_only_bytes = (second_aggregate.read_bytes(), second_combined.read_bytes())
    assert pending.read_bytes() == pending_before
    blocked = run_script(
        "step3_aggregate.py", "--date", second_date, env_extra=environment
    )
    assert blocked.returncode == 1
    assert first_date in blocked.stdout
    assert pending.read_bytes() == pending_before
    assert (second_aggregate.read_bytes(), second_combined.read_bytes()) == read_only_bytes
    wrong_commit = run_script(
        "step2_save_state.py",
        "--date",
        second_date,
        "--commit-pending",
        env_extra=environment,
    )
    assert wrong_commit.returncode == 1
    assert first_date in wrong_commit.stdout
    assert pending.read_bytes() == pending_before

    committed = run_script(
        "step2_save_state.py",
        "--date",
        first_date,
        "--commit-pending",
        env_extra=environment,
    )
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "__pillar_b__": ["https://example.org/first"]
    }
    assert not pending.exists()


def test_legacy_same_date_does_not_overwrite_completed_unapplied_pending(reports_dir):
    report_date = "2026-08-31"
    state_file = reports_dir / "state" / "article_state.json"
    state_file.parent.mkdir()
    state_file.write_bytes(b"{}\n")
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        article_changes(report_date, []),
    )
    write_json(
        reports_dir / f"pillar_b_{report_date}.json",
        [
            {
                "title": "Climate insurance protected report",
                "url": "https://example.org/protected",
                "source": "web",
                "summary": "Protected search-result evidence.",
            }
        ],
    )
    environment = {
        "CLIMATE_REPORTS_DIR": str(reports_dir),
        "CLIMATE_WL_STATE": str(state_file),
    }
    for script in ("step3_aggregate.py", "step3_filter.py", "step5_build_md.py"):
        result = run_script(script, "--date", report_date, env_extra=environment)
        assert result.returncode == 0, result.stdout + result.stderr
    pending = state_file.with_name(state_file.name + ".pending-urls.json")
    protected = {
        "pending": pending.read_bytes(),
        "combined": (reports_dir / f"combined-candidates_{report_date}.json").read_bytes(),
        "report": (reports_dir / f"climate-monitor-{report_date}.md").read_bytes(),
        "evidence": (reports_dir / f"climate-monitor-{report_date}.json").read_bytes(),
        "state": state_file.read_bytes(),
    }

    blocked = run_script(
        "step3_aggregate.py", "--date", report_date, env_extra=environment
    )

    assert blocked.returncode == 1
    assert "--commit-pending" in blocked.stdout
    assert pending.read_bytes() == protected["pending"]
    assert (reports_dir / f"combined-candidates_{report_date}.json").read_bytes() == protected["combined"]
    assert (reports_dir / f"climate-monitor-{report_date}.md").read_bytes() == protected["report"]
    assert (reports_dir / f"climate-monitor-{report_date}.json").read_bytes() == protected["evidence"]
    assert state_file.read_bytes() == protected["state"]

    committed = run_script(
        "step2_save_state.py",
        "--date",
        report_date,
        "--commit-pending",
        env_extra=environment,
    )
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "__pillar_b__": ["https://example.org/protected"]
    }


def test_legacy_malformed_replay_preserves_committed_bundle_but_invalidates_aggregate(
    reports_dir,
):
    report_date = "2026-08-31"
    state_file = reports_dir / "state" / "article_state.json"
    state_file.parent.mkdir()
    state_file.write_bytes(b"{}\n")
    write_json(
        reports_dir / f"article_changes_{report_date}.json",
        article_changes(report_date, []),
    )
    pillar_b = reports_dir / f"pillar_b_{report_date}.json"
    write_json(
        pillar_b,
        [
            {
                "title": "Climate insurance committed report",
                "url": "https://example.org/committed",
                "source": "web",
                "summary": "Committed search-result evidence.",
            }
        ],
    )
    _run_legacy_report_cycle(reports_dir, state_file, report_date)
    aggregate = reports_dir / f"aggregated_{report_date}.json"
    combined = reports_dir / f"combined-candidates_{report_date}.json"
    report = reports_dir / f"climate-monitor-{report_date}.md"
    evidence = report.with_suffix(".json")
    protected = (
        combined.read_bytes(),
        report.read_bytes(),
        evidence.read_bytes(),
        state_file.read_bytes(),
    )
    write_json(
        pillar_b,
        [
            {
                "title": "Malformed replacement",
                "url": "https://example.org/replacement",
                "source": "not-web",
                "summary": "Malformed replacement.",
            }
        ],
    )

    failed = run_script(
        "step3_aggregate.py",
        "--date",
        report_date,
        env_extra={
            "CLIMATE_REPORTS_DIR": str(reports_dir),
            "CLIMATE_WL_STATE": str(state_file),
        },
    )

    assert failed.returncode == 1
    assert "invalid_source=1" in failed.stdout
    assert not aggregate.exists()
    assert (
        combined.read_bytes(),
        report.read_bytes(),
        evidence.read_bytes(),
        state_file.read_bytes(),
    ) == protected


def test_step1_preserves_semantic_query_and_does_not_write_seen_state(
    reports_dir, tmp_path, monkeypatch
):
    from scripts import step1_pillar_a

    database = tmp_path / "web_listening.db"
    state = tmp_path / "article_state.json"
    output = reports_dir / "article_changes_2026-09-14.json"
    state.write_bytes(
        b'{"Example Org":["https://example.org/report?edition=2026"]}\r\n'
    )
    before = state.read_bytes()
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE sites(id INTEGER PRIMARY KEY, name TEXT, url TEXT);
            CREATE TABLE changes(
                site_id INTEGER, detected_at TEXT, change_type TEXT, diff_snippet TEXT
            );
            INSERT INTO sites VALUES(1, 'Example Org', 'https://example.org');
            """
        )
        connection.execute(
            "INSERT INTO changes VALUES(?, ?, ?, ?)",
            (
                1,
                "2026-09-14T08:00:00Z",
                "new_content",
                "+#### [Climate insurance report]"
                "(https://example.org/report?edition=2026&utm_source=mail#findings)",
            ),
        )

    monkeypatch.setattr(step1_pillar_a, "SITE_DB", database)
    monkeypatch.setattr(step1_pillar_a, "STATE_FILE", state)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "step1_pillar_a.py",
            "--date",
            "2026-09-14",
            "--since-days",
            "7",
            "--output",
            str(output),
        ],
    )

    assert step1_pillar_a.main() is None
    assert state.read_bytes() == before
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["seen_before"] == 1
    assert payload["new_articles"] == 1
    assert payload["articles"][0]["items"][0]["url"] == (
        "https://example.org/report?edition=2026"
    )

    write_json(
        reports_dir / "pillar_b_2026-09-14.json",
        [{
            "title": "Search rediscovery",
            "url": "https://example.org/report?edition=2026&fbclid=search#abstract",
            "source": "web",
            "summary": "Climate insurance search evidence.",
        }],
    )
    result = run_script(
        "step3_aggregate.py",
        "--date",
        "2026-09-14",
        env_extra={
            "CLIMATE_REPORTS_DIR": str(reports_dir),
            "CLIMATE_WL_STATE": str(state),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert state.read_bytes() == before
    combined = json.loads(
        (reports_dir / "combined-candidates_2026-09-14.json").read_text(encoding="utf-8")
    )
    assert combined["counts"] == {
        "pillar_a_rows": 1,
        "pillar_b_rows": 1,
        "unique_urls": 1,
        "cross_pillar_merges": 1,
        "history_skips": 1,
        "invalid_rows": 0,
    }
    assert combined["items"] == []
    assert [origin["pillar"] for origin in combined["history_skips"][0]["origins"]] == [
        "A",
        "B",
    ]


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
    allow = ["--allow-future", "--allow-offcycle"]
    result = run_script("step5_build_md.py", "--date", "2026-09-14",
                        *allow, env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)})
    assert result.returncode == 0, result.stdout + result.stderr
    md = (reports_dir / "climate-monitor-2026-09-14.md").read_text()
    assert "### Catastrophe & NatCat (1)" in md
    assert "### Scenario Analysis (1)" not in md  # primary grouping only
    assert "**Categories:** Catastrophe & NatCat, Scenario Analysis, Financial Risk" in md
    sidecar = json.loads((reports_dir / "climate-monitor-2026-09-14.json").read_text())
    item = sidecar["categories"]["catastrophe_natcat"][0]
    assert item["categories"] == ["Catastrophe & NatCat", "Scenario Analysis", "Financial Risk"]


def test_step5_help_describes_validated_monitor_result_and_unknown_fallback():
    result = run_script("step5_build_md.py", "--help")
    assert result.returncode == 0
    help_text = " ".join(result.stdout.split())
    assert "Validated weekly-run-attempt.v1" in help_text
    assert "unknown" in help_text
    assert "--combined" in help_text
    assert ".next sibling" in help_text


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


def test_step8_requires_deployed_source_and_is_rerun_safe(reports_dir, tmp_path):
    """Registry sync must never promote a generated candidate into sources/.

    The report becomes eligible only after the rolling PR is merged and its
    tracked source is deployed. The registry DB does not exist beforehand, so
    the accepted path must still bootstrap the first-deploy schema.
    """
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
    assert result.returncode == 1
    assert "not present in deployed sources/" in result.stderr
    assert not (sources / "climate-monitor-2026-09-14.md").exists()
    assert not db_path.exists()

    sources.mkdir()
    deployed_report = sources / "climate-monitor-2026-09-14.md"
    deployed_report.write_text(WEEKLY_MD)
    before = deployed_report.read_bytes()

    result = run_script("step8_sync_registry.py", "--date", "2026-09-14",
                        "--allow-future", "--allow-offcycle", env_extra=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Initialized registry database" in result.stdout
    assert deployed_report.read_bytes() == before
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


def test_step9_delegates_to_rolling_publisher_without_touching_sources(
    reports_dir, tmp_path, monkeypatch, capsys
):
    report_date = "2026-09-03"
    (reports_dir / f"climate-monitor-{report_date}.md").write_text(
        WEEKLY_MD.replace("2026-09-14", report_date)
    )
    publisher = tmp_path / "scripts" / "weekly_wiki_refresh.sh"
    publisher.parent.mkdir()
    publisher.write_text("#!/usr/bin/env bash\n")
    observed = {}

    def fake_run(cmd, *, timeout=900, env=None):
        observed.update(cmd=cmd, timeout=timeout, env=env)
        return subprocess.CompletedProcess(cmd, 0, stdout="published\n", stderr="")

    monkeypatch.setattr(step9_update_website, "HOME", tmp_path)
    monkeypatch.setattr(step9_update_website, "REPORTS", reports_dir)
    monkeypatch.setattr(step9_update_website, "PYTHON", Path(PYTHON))
    monkeypatch.setattr(step9_update_website, "PUBLISHER", publisher)
    monkeypatch.setattr(step9_update_website, "run", fake_run)
    monkeypatch.setenv("RELOAD_TOKEN", "must-not-reach-publisher")
    monkeypatch.setattr(
        sys,
        "argv",
        ["step9_update_website.py", "--date", report_date, "--allow-offcycle"],
    )

    assert step9_update_website.main() == 0
    assert observed["cmd"] == ["bash", str(publisher)]
    assert observed["env"]["REPORT_DIR"] == str(reports_dir)
    assert observed["env"]["REPO"] == str(tmp_path)
    assert observed["env"]["CLIMATE_PUBLISH_REPORT_DATE"] == report_date
    assert observed["env"]["CLIMATE_PUBLISH_ALLOW_OFFCYCLE"] == "1"
    assert "RELOAD_TOKEN" not in observed["env"]
    assert not (tmp_path / "sources").exists()
    assert "merge and deployment are still required" in capsys.readouterr().out


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
