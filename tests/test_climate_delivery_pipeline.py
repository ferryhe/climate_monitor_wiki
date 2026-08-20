import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from climate_delivery.cli import main
from climate_delivery.errors import DeliveryError, GenerationError, InputError, LockStateError
from climate_delivery.pipeline import run_delivery
from climate_delivery.report import parse_weekly_report
from climate_monitor.semantic_bundle import (
    build_sidecar_payload,
    serialize_sidecar,
    semantic_sidecar_path,
)
from climate_monitor.taxonomy import load_article_taxonomy

from test_climate_delivery_email import config_file
from test_climate_delivery_report import REPORT, report_file


# --- Semantic sidecar fixtures (PR-C consumer contract) ---------------------
#
# The 09:00 delivery now requires the SHA-bound semantic sidecar that the 08:00
# producer commits next to the canonical Markdown. These helpers build a report
# in the canonical ``**URL:**`` format (so the sidecar's 1:1 URL binding holds)
# together with a valid, taxonomy-verified sidecar. Reusing them keeps every
# pre-existing pipeline test exercising the real disk-backed verify path.

DELIVERY_REPORT = (
    REPORT.replace("  🔗 https://example.test/first", "**URL:** https://example.test/first <br>")
    .replace("  🔗 https://example.test/second", "**URL:** https://example.test/second <br>")
)


def _sidecar_items() -> list[dict]:
    return [
        {
            "url": "https://example.test/first",
            "title": "First finding",
            "lane": "website",
            "source_name": "Example",
            "content_hash": "f" * 64,
            "semantics": {
                "summary": "First article semantic summary.",
                "categories": ["Physical Risk", "Insurance Risk"],
                "keywords": ["flood", "pricing", "resilience"],
            },
        },
        {
            "url": "https://example.test/second",
            "title": "Second finding",
            "lane": "website",
            "source_name": "Example",
            "content_hash": "s" * 64,
            "semantics": {
                "summary": "Second article semantic summary.",
                "categories": ["Supervision & Disclosure", "Capital & Solvency"],
                "keywords": ["disclosure", "capital", "supervision"],
            },
        },
    ]


def _write_sidecar(report_path: Path) -> None:
    report_path = Path(report_path)
    report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    payload = build_sidecar_payload(
        report_date=date(2026, 8, 10),
        report_filename=report_path.name,
        report_sha256=report_sha256,
        items=_sidecar_items(),
        taxonomy=load_article_taxonomy(),
    )
    semantic_sidecar_path(report_path).write_bytes(serialize_sidecar(payload))


def delivery_report(tmp_path: Path, text: str = DELIVERY_REPORT, name: str = "climate-monitor-2026-08-10.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    _write_sidecar(path)
    return path


def configure_env(monkeypatch):
    for key, value in {
        "TEST_SMTP_HOST": "smtp.example.test",
        "TEST_SMTP_PORT": "587",
        "TEST_SMTP_USER": "sender-user",
        "TEST_SMTP_PASSWORD": "not-a-real-password",
        "TEST_FROM_ADDRESS": "sender@example.test",
    }.items():
        monkeypatch.setenv(key, value)


def test_run_writes_content_addressed_artifacts_and_redacted_manifest(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output = tmp_path / "output"
    state = tmp_path / "state"

    result = run_delivery(report, output, state, config_file(tmp_path), dry_run=True)

    artifact_dir = output / "2026-08-10" / result["report_sha256"]
    pdf_name = "climate-monitor-2026-08-10.pdf"
    assert sorted(path.name for path in artifact_dir.iterdir()) == [pdf_name, "manifest.json", "summary.json"]
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    rendered = json.dumps(manifest)
    assert manifest["schema_version"] == 1
    assert manifest["delivery"]["status"] == "dry-run"
    assert [r["id"] for r in manifest["delivery"]["recipients"]] == ["alpha", "beta", "gamma", "delta"]
    assert manifest["artifacts"]["summary"]["path"] == "summary.json"
    assert manifest["artifacts"]["pdf"]["path"] == pdf_name
    assert manifest["artifacts"]["summary"]["sha256"] == __import__("hashlib").sha256(
        (artifact_dir / "summary.json").read_bytes()
    ).hexdigest()
    assert manifest["artifacts"]["pdf"]["sha256"] == __import__("hashlib").sha256(
        (artifact_dir / pdf_name).read_bytes()
    ).hexdigest()
    assert all(set(item) == {"id", "status"} for item in manifest["delivery"]["recipients"])
    known_fingerprint = hashlib.sha256("alpha@example.test".encode()).hexdigest()
    assert "alpha@example.test" not in rendered
    assert known_fingerprint not in rendered
    assert "example.test" not in rendered
    assert str(tmp_path) not in rendered
    assert not list(output.rglob("*.tmp"))
    assert not list(state.rglob("*.lock")) if state.exists() else True

    second_pdf = tmp_path / "second.pdf"
    from climate_delivery.pdf import render_pdf

    render_pdf(json.loads((artifact_dir / "summary.json").read_text(encoding="utf-8")), second_pdf)
    assert second_pdf.read_bytes() == (artifact_dir / pdf_name).read_bytes()


def test_existing_run_lock_fails_without_force(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    digest = parse_weekly_report(report).sha256
    state = tmp_path / "state"
    locks = state / "locks"
    locks.mkdir(parents=True)
    (locks / f"{digest}.lock").write_text("occupied", encoding="ascii")

    with pytest.raises(LockStateError, match="locked"):
        run_delivery(report, tmp_path / "output", state, config_file(tmp_path), dry_run=True)


def test_same_date_changed_report_uses_a_new_content_addressed_directory(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output = tmp_path / "output"
    state = tmp_path / "state"
    config = config_file(tmp_path)
    first = run_delivery(report, output, state, config, dry_run=True)

    report.write_text(
        DELIVERY_REPORT.replace("One deterministic observation.", "A changed deterministic observation."),
        encoding="utf-8",
    )
    _write_sidecar(report)
    second = run_delivery(report, output, state, config, dry_run=True)

    assert first["report_sha256"] != second["report_sha256"]
    date_dir = output / "2026-08-10"
    assert {path.name for path in date_dir.iterdir()} == {first["report_sha256"], second["report_sha256"]}


@pytest.mark.parametrize("changed", ["summary", "pdf"])
def test_existing_content_addressed_artifacts_are_never_overwritten(tmp_path, monkeypatch, changed):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output = tmp_path / "output"
    state = tmp_path / "state"
    config = config_file(tmp_path)
    first = run_delivery(report, output, state, config, dry_run=True)
    artifact_dir = output / "2026-08-10" / first["report_sha256"]
    before = {path.name: path.read_bytes() for path in artifact_dir.iterdir() if path.name != "manifest.json"}

    if changed == "summary":
        original = __import__("climate_delivery.pipeline", fromlist=["build_summary"]).build_summary

        def changed_summary(report_value):
            value = original(report_value)
            value["executive_summary"].append("implementation changed")
            return value

        monkeypatch.setattr("climate_delivery.pipeline.build_summary", changed_summary)
    else:
        monkeypatch.setattr("climate_delivery.pipeline.render_pdf", lambda summary, path: path.write_bytes(b"different pdf"))

    with pytest.raises(LockStateError, match="artifact"):
        run_delivery(report, output, state, config, dry_run=True)
    assert {name: (artifact_dir / name).read_bytes() for name in before} == before


def test_pipeline_requires_external_absolute_non_nested_paths(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    valid_inputs = tmp_path / "valid-inputs"
    valid_inputs.mkdir()
    report = delivery_report(valid_inputs)
    config = config_file(valid_inputs)
    with pytest.raises(InputError, match="nested|separate"):
        run_delivery(report, tmp_path / "work", tmp_path / "work" / "state", config, dry_run=True)

    repo_report = Path(__file__).parents[1] / "sources" / "climate-monitor-2026-08-10.md"
    with pytest.raises(InputError, match="repository"):
        run_delivery(repo_report, tmp_path / "output", tmp_path / "state", config, dry_run=True)


@pytest.mark.parametrize("conflict", ["output", "state"])
def test_pipeline_rejects_existing_file_where_directory_root_is_required(tmp_path, monkeypatch, conflict):
    configure_env(monkeypatch)
    valid_cli_inputs = tmp_path / "valid-cli-inputs"
    valid_cli_inputs.mkdir()
    report = delivery_report(valid_cli_inputs)
    config = config_file(valid_cli_inputs)
    output = tmp_path / "output"
    state = tmp_path / "state"
    selected = output if conflict == "output" else state
    selected.write_text("not a directory", encoding="utf-8")

    with pytest.raises(InputError, match=f"{conflict}.*directory"):
        run_delivery(report, output, state, config, dry_run=True)


@pytest.mark.parametrize("argument", ["report", "config"])
def test_pipeline_rejects_existing_directory_where_file_is_required(tmp_path, monkeypatch, argument):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    config = config_file(tmp_path)
    selected = report if argument == "report" else config
    selected.unlink()
    selected.mkdir()

    with pytest.raises(InputError, match=f"{argument}.*file"):
        run_delivery(report, tmp_path / "output", tmp_path / "state", config, dry_run=True)


def test_pipeline_preserves_original_error_when_failure_manifest_cannot_be_written(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    original = LockStateError("original ambiguous state")
    manifest_error = OSError("manifest write failed")
    monkeypatch.setattr("climate_delivery.pipeline.deliver", lambda *args, **kwargs: (_ for _ in ()).throw(original))
    monkeypatch.setattr(
        "climate_delivery.pipeline.atomic_write_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(manifest_error),
    )

    with pytest.raises(LockStateError, match="original ambiguous") as raised:
        run_delivery(report, tmp_path / "output", tmp_path / "state", config_file(tmp_path))
    assert raised.value is original
    assert raised.value.__cause__ is manifest_error


@pytest.mark.parametrize(
    ("error", "recipient_status", "manifest_status"),
    [
        (DeliveryError("explicit rejection"), "failed", "failed"),
        (LockStateError("unknown outcome"), "unknown", "ambiguous"),
    ],
)
def test_failure_manifest_distinguishes_known_and_ambiguous_outcomes(
    tmp_path, monkeypatch, error, recipient_status, manifest_status
):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output = tmp_path / "output"
    state_dir = tmp_path / "state"
    parsed = parse_weekly_report(report)
    state_dir.mkdir()
    state = {
        "schema_version": 1,
        "report_sha256": parsed.sha256,
        "recipients": {
            recipient_id: {"status": recipient_status if recipient_id == "alpha" else "pending"}
            for recipient_id in ("alpha", "beta", "gamma", "delta")
        },
    }
    (state_dir / f"{parsed.sha256}.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr("climate_delivery.pipeline.deliver", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(type(error)):
        run_delivery(report, output, state_dir, config_file(tmp_path))
    manifest = json.loads(next(output.rglob("manifest.json")).read_text(encoding="utf-8"))
    assert manifest["delivery"]["status"] == manifest_status
    assert manifest["delivery"]["recipients"][0] == {"id": "alpha", "status": recipient_status}


@pytest.mark.parametrize(
    ("argv", "exit_code", "kind"),
    [
        (["summarize"], 2, "input"),
        (["summarize", "--report", "missing.md", "--output", "out.json"], 2, "input"),
    ],
)
def test_cli_has_stable_redacted_json_errors(argv, exit_code, kind, capsys):
    assert main(argv) == exit_code
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "error"
    assert payload["kind"] == kind


def test_cli_rejects_repo_internal_and_relative_operation_paths(tmp_path, capsys):
    repo_report = Path(__file__).parents[1] / "sources" / "climate-monitor-2026-08-10.md"
    assert main(["summarize", "--report", str(repo_report), "--output", str(tmp_path / "summary.json")]) == 2
    assert json.loads(capsys.readouterr().out)["kind"] == "input"


def test_cli_returns_exit2_for_path_type_conflicts(tmp_path, capsys):
    report_dir = tmp_path / "climate-monitor-2026-08-10.md"
    report_dir.mkdir()
    output = tmp_path / "summary.json"
    assert main(["summarize", "--report", str(report_dir), "--output", str(output)]) == 2
    assert json.loads(capsys.readouterr().out)["kind"] == "input"

    summary = tmp_path / "external-summary.json"
    summary.write_text("{}", encoding="utf-8")
    output_dir = tmp_path / "pdf-output"
    output_dir.mkdir()
    assert main(["render-pdf", "--summary", str(summary), "--output", str(output_dir)]) == 2
    assert json.loads(capsys.readouterr().out)["kind"] == "input"

    cli_inputs = tmp_path / "cli-run-inputs"
    cli_inputs.mkdir()
    report = delivery_report(cli_inputs)
    config = config_file(cli_inputs)
    for conflict in ("output", "state"):
        output_root = tmp_path / f"{conflict}-output-root"
        state_root = tmp_path / f"{conflict}-state-root"
        selected = output_root if conflict == "output" else state_root
        selected.write_text("not a directory", encoding="utf-8")
        assert main(
            [
                "run",
                "--report",
                str(report),
                "--output-dir",
                str(output_root),
                "--state-dir",
                str(state_root),
                "--config",
                str(config),
                "--dry-run",
            ]
        ) == 2
        assert json.loads(capsys.readouterr().out)["kind"] == "input"
    assert main(["send-email", "--summary", "relative.json", "--pdf", "relative.pdf", "--config", "config.yaml", "--state-dir", "state"]) == 2
    assert json.loads(capsys.readouterr().out)["kind"] == "input"


def test_cli_requires_explicit_paths_and_dry_run_succeeds(tmp_path, monkeypatch, capsys):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    result = main(
        [
            "run",
            "--report",
            str(report),
            "--output-dir",
            str(tmp_path / "output"),
            "--state-dir",
            str(tmp_path / "state"),
            "--config",
            str(config_file(tmp_path)),
            "--dry-run",
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry-run"
    assert "example.test" not in json.dumps(payload)


def test_send_email_cli_stdout_excludes_recipient_address_and_fingerprint(tmp_path, monkeypatch, capsys):
    configure_env(monkeypatch)
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report": {"date": "2026-08-10", "title": "Weekly", "sha256": "a" * 64},
                "executive_summary": ["Summary"],
                "highlights": [
                    {"pillar": "A", "title": "Finding", "summary": "Evidence", "url": "https://source.invalid/a"}
                ],
                "original_links": ["https://source.invalid/a"],
            }
        ),
        encoding="utf-8",
    )
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    assert main(
        [
            "send-email",
            "--summary",
            str(summary_path),
            "--pdf",
            str(pdf),
            "--config",
            str(config_file(tmp_path)),
            "--state-dir",
            str(tmp_path / "state"),
            "--dry-run",
        ]
    ) == 0
    rendered = capsys.readouterr().out
    assert "alpha@example.test" not in rendered
    assert hashlib.sha256("alpha@example.test".encode()).hexdigest() not in rendered


def test_render_pdf_cli_consumes_explicit_summary(tmp_path, capsys):
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report": {"date": "2026-08-10", "title": "Weekly", "sha256": "a" * 64},
                "executive_summary": ["Summary"],
                "highlights": [
                    {"pillar": "A", "title": "Finding", "summary": "Evidence", "url": "https://example.test/a"}
                ],
                "original_links": ["https://example.test/a"],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report.pdf"
    assert main(["render-pdf", "--summary", str(summary_path), "--output", str(output)]) == 0
    assert output.read_bytes().startswith(b"%PDF")
    assert json.loads(capsys.readouterr().out)["status"] == "success"


@pytest.mark.parametrize(
    ("exception", "exit_code", "kind"),
    [
        (GenerationError("no pdf"), 3, "generation"),
        (DeliveryError("smtp failed"), 4, "delivery"),
        (LockStateError("ambiguous"), 5, "lock-state"),
    ],
)
def test_cli_exit_code_contract(monkeypatch, capsys, tmp_path, exception, exit_code, kind):
    monkeypatch.setattr("climate_delivery.cli.run_delivery", lambda *args, **kwargs: (_ for _ in ()).throw(exception))
    result = main(
        [
            "run",
            "--report",
            str(tmp_path / "input.md"),
            "--output-dir",
            str(tmp_path / "output"),
            "--state-dir",
            str(tmp_path / "state"),
            "--config",
            str(tmp_path / "config.yaml"),
        ]
    )
    assert result == exit_code
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "error", "kind": kind, "message": str(exception)}
