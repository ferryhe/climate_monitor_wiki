"""PR-C: 09:00 delivery consumes the same SHA-bound article semantic bundle.

These tests pin the consumer contract introduced alongside the 08:00 producer
(``climate_monitor.semantic_bundle``): the 09:00 ``run_delivery`` must load and
verify the sidecar that is bound to the canonical Markdown SHA-256, fail closed
when the sidecar is missing / mismatched / tampered, and inject the *verified*
semantics (categories, keywords) into both ``summary.json`` and the rendered
PDF. There is no Markdown-scrape fallback and no model/LLM call on the
production path.
"""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfReader

from climate_delivery.delivery import load_summary_with_sha256
from climate_delivery.pipeline import run_delivery
from climate_monitor.semantic_bundle import (
    SemanticBundleError,
    build_sidecar_payload,
    serialize_sidecar,
    semantic_sidecar_path,
)
from climate_monitor.taxonomy import load_article_taxonomy

from test_climate_delivery_email import config_file
from test_climate_delivery_pipeline import (
    DELIVERY_REPORT,
    _sidecar_items,
    _write_sidecar,
    delivery_report,
)


def configure_env(monkeypatch):
    for key, value in {
        "TEST_SMTP_HOST": "smtp.example.test",
        "TEST_SMTP_PORT": "587",
        "TEST_SMTP_USER": "sender-user",
        "TEST_SMTP_PASSWORD": "not-a-real-password",
        "TEST_FROM_ADDRESS": "sender@example.test",
    }.items():
        monkeypatch.setenv(key, value)


def _report_only(tmp_path: Path, text: str = DELIVERY_REPORT, name: str = "climate-monitor-2026-08-10.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _valid_payload(report_path: Path) -> dict:
    report_path = Path(report_path)
    report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    return build_sidecar_payload(
        report_date=date(2026, 8, 10),
        report_filename=report_path.name,
        report_sha256=report_sha256,
        items=_sidecar_items(),
        taxonomy=load_article_taxonomy(),
    )


def _write_payload(report_path: Path, payload: dict) -> None:
    semantic_sidecar_path(Path(report_path)).write_bytes(serialize_sidecar(payload))


def _pdf_text(pdf_path: Path) -> str:
    reader = PdfReader(Path(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages).casefold()


def _artifact_dir(output: Path, result: dict) -> Path:
    return output / "2026-08-10" / result["report_sha256"]


# ---------------------------------------------------------------------------
# Fail-closed regression: a report cannot be delivered without a verifiable
# semantic contract.
# ---------------------------------------------------------------------------


def test_run_delivery_aborts_when_sidecar_missing(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = _report_only(tmp_path)
    assert not semantic_sidecar_path(report).exists()

    with pytest.raises(SemanticBundleError, match="missing"):
        run_delivery(report, tmp_path / "output", tmp_path / "state", config_file(tmp_path), dry_run=True)


def test_run_delivery_aborts_when_sidecar_bound_to_different_sha256(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = _report_only(tmp_path)
    payload = _valid_payload(report)
    payload["report"]["sha256"] = "0" * 64  # bind to a bogus report SHA
    _write_payload(report, payload)

    with pytest.raises(SemanticBundleError, match="sha256|bound"):
        run_delivery(report, tmp_path / "output", tmp_path / "state", config_file(tmp_path), dry_run=True)


def test_run_delivery_aborts_when_sidecar_tampered(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = _report_only(tmp_path)
    payload = _valid_payload(report)
    # Tamper an article's category into a label outside the taxonomy so the
    # per-article bundle validation fails closed.
    payload["articles"][0]["semantics"]["categories"] = ["Not A Taxonomy Category"]
    _write_payload(report, payload)

    with pytest.raises(SemanticBundleError, match="contract-valid|taxonomy"):
        run_delivery(report, tmp_path / "output", tmp_path / "state", config_file(tmp_path), dry_run=True)


def test_run_delivery_aborts_when_sidecar_filename_mismatched(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = _report_only(tmp_path, name="climate-monitor-2026-08-10.md")
    # A sidecar bound to a different report filename must be rejected.
    payload = _valid_payload(report)
    payload["report"]["filename"] = "climate-monitor-2026-08-17.md"
    _write_payload(report, payload)

    with pytest.raises(SemanticBundleError, match="filename|bound"):
        run_delivery(report, tmp_path / "output", tmp_path / "state", config_file(tmp_path), dry_run=True)


def test_fail_closed_does_not_write_artifacts(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = _report_only(tmp_path)
    output = tmp_path / "output"

    with pytest.raises(SemanticBundleError):
        run_delivery(report, output, tmp_path / "state", config_file(tmp_path), dry_run=True)

    assert not any(output.rglob("summary.json"))
    assert not any(output.rglob("*.pdf"))


# ---------------------------------------------------------------------------
# Happy path: a verified sidecar is consumed into summary.json and the PDF.
# ---------------------------------------------------------------------------


def test_happy_path_injects_verified_semantics_into_summary_json(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output = tmp_path / "output"
    state = tmp_path / "state"

    result = run_delivery(report, output, state, config_file(tmp_path), dry_run=True)
    summary = json.loads((_artifact_dir(output, result) / "summary.json").read_text(encoding="utf-8"))

    assert summary["schema_version"] == 1
    # The v1 highlight shape is preserved exactly.
    assert all(set(item) == {"pillar", "title", "summary", "url"} for item in summary["highlights"])
    # Verified semantics travel alongside the highlights.
    assert summary["article_semantics"]["https://example.test/first"]["categories"] == [
        "Physical Risk",
        "Insurance Risk",
    ]
    assert "flood" in summary["article_semantics"]["https://example.test/first"]["keywords"]
    assert summary["article_semantics"]["https://example.test/second"]["categories"] == [
        "Supervision & Disclosure",
        "Capital & Solvency",
    ]


def test_happy_path_renders_verified_semantics_into_pdf(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output = tmp_path / "output"
    state = tmp_path / "state"

    result = run_delivery(report, output, state, config_file(tmp_path), dry_run=True)

    pdf_path = _artifact_dir(output, result) / "climate-monitor-2026-08-10.pdf"
    text = _pdf_text(pdf_path)

    # Distinctive verified tokens must appear in the rendered PDF text.
    assert "categories:" in text
    assert "keywords:" in text
    assert "flood" in text
    assert "resilience" in text
    assert "physical risk" in text
    assert "supervision" in text


def test_happy_path_summary_still_passes_contract_validation(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output = tmp_path / "output"
    state = tmp_path / "state"

    result = run_delivery(report, output, state, config_file(tmp_path), dry_run=True)

    # load_summary_with_sha256 runs _validate_summary; the extra article_semantics
    # key must not break validation.
    summary, _ = load_summary_with_sha256(_artifact_dir(output, result) / "summary.json")
    assert summary["article_semantics"]


# ---------------------------------------------------------------------------
# Test-only override path: exercises injection without a disk sidecar, and
# proves the production path (override=None) still requires the disk verify.
# ---------------------------------------------------------------------------


def test_override_injects_semantics_without_disk_sidecar(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = _report_only(tmp_path)  # no sidecar on disk
    payload = _valid_payload(report)

    result = run_delivery(
        report,
        tmp_path / "output",
        tmp_path / "state",
        config_file(tmp_path),
        dry_run=True,
        semantics_override=payload,
    )

    assert result["status"] == "dry-run"
    summary = json.loads((_artifact_dir(tmp_path / "output", result) / "summary.json").read_text(encoding="utf-8"))
    assert summary["article_semantics"]["https://example.test/first"]["keywords"]
    pdf_text = _pdf_text(_artifact_dir(tmp_path / "output", result) / "climate-monitor-2026-08-10.pdf")
    assert "flood" in pdf_text


def test_production_path_ignores_override_default_and_requires_sidecar(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = _report_only(tmp_path)  # no sidecar on disk
    # With the override default (None) the real disk verify runs and fails closed.
    with pytest.raises(SemanticBundleError, match="missing"):
        run_delivery(report, tmp_path / "output", tmp_path / "state", config_file(tmp_path), dry_run=True)


# ---------------------------------------------------------------------------
# Determinism: identical inputs produce byte-identical artifacts.
# ---------------------------------------------------------------------------


def test_delivery_is_deterministic_across_runs(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output = tmp_path / "output"
    state = tmp_path / "state"

    first = run_delivery(report, output, state, config_file(tmp_path), dry_run=True)
    second = run_delivery(report, output, state, config_file(tmp_path), dry_run=True)

    assert first["report_sha256"] == second["report_sha256"]
    artifact_dir = _artifact_dir(output, first)
    summary_a = (artifact_dir / "summary.json").read_bytes()
    pdf_a = (artifact_dir / "climate-monitor-2026-08-10.pdf").read_bytes()

    output_b = tmp_path / "output-b"
    run_delivery(report, output_b, tmp_path / "state-b", config_file(tmp_path), dry_run=True)
    artifact_dir_b = _artifact_dir(output_b, first)
    assert (artifact_dir_b / "summary.json").read_bytes() == summary_a
    assert (artifact_dir_b / "climate-monitor-2026-08-10.pdf").read_bytes() == pdf_a
