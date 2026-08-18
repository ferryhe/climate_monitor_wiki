import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
import climate_delivery.backfill as backfill_module
from climate_delivery.artifacts import load_report_artifact
from climate_delivery.backfill import backfill_reports
from climate_delivery.cli import main
from climate_delivery.errors import GenerationError, InputError
from climate_monitor.dedupe import canonical_url
from climate_registry.annotations import load_article_annotations
from climate_registry.audit import build_audit_registry


def _item(title: str, summary: str, url: str) -> str:
    return f"- **{title}** (web)\n  - {summary}\n  🔗 {url}\n"


def _weekly(
    report_date: str,
    *,
    a_items: tuple[tuple[str, str, str], ...],
    b_items: tuple[tuple[str, str, str], ...],
    sites: tuple[int, int, int] = (8, 7, 1),
    include_monitoring: bool = True,
) -> str:
    checked, succeeded, failed = sites
    monitoring = (
        f"- Sites checked: **{checked}**, succeeded: **{succeeded}**, failed: **{failed}**\n"
        if include_monitoring
        else "- Monitoring completed without recorded counts.\n"
    )
    return (
        "# Weekly Climate & Actuarial Monitor\n"
        f"**Report Date:** {report_date}\n"
        "## Executive Summary\n"
        f"{monitoring}"
        "- Monitoring remained focused on source-backed actuarial developments.\n"
        "## Pillar A — Changes\n"
        + "".join(_item(*item) for item in a_items)
        + "## Pillar B — Intelligence\n"
        + "".join(_item(*item) for item in b_items)
        + "## Original Links\n"
        + "".join(f"- {item[2]}\n" for item in (*a_items, *b_items))
    )


def _write_report(
    sources: Path,
    report_date: str,
    *,
    a_count: int = 1,
    b_count: int = 1,
    sites: tuple[int, int, int] = (8, 7, 1),
) -> Path:
    sources.mkdir(parents=True, exist_ok=True)
    a_items = tuple(
        (f"Source A{index}", f"Source summary A{index}.", f"https://a{index}.example.test/{report_date}")
        for index in range(1, a_count + 1)
    )
    b_items = tuple(
        (f"Source B{index}", f"Source summary B{index}.", f"https://b{index}.example.test/{report_date}")
        for index in range(1, b_count + 1)
    )
    path = sources / f"climate-monitor-{report_date}.md"
    path.write_text(
        _weekly(report_date, a_items=a_items, b_items=b_items, sites=sites),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _report_articles(path: Path) -> list[dict[str, object]]:
    from climate_delivery.report import parse_weekly_report

    report = parse_weekly_report(path)
    return [
        {
            "canonical_url": canonical_url(item.url),
            "source_url": item.url,
            "title": f"Canonical {item.title}",
            "source_basis": "report_fallback",
            "summary": f"Canonical evidence for {item.title}.",
            "categories": ["Climate Risk"],
            "keywords": ["actuarial evidence", "climate monitoring", "source identity"],
        }
        for item in report.highlights
    ]


def _write_annotations(metadata: Path, reports: list[Path], *, omit_last: bool = False) -> None:
    metadata.mkdir(parents=True, exist_ok=True)
    articles: dict[str, dict[str, object]] = {}
    for report in reports:
        for item in _report_articles(report):
            articles[str(item["canonical_url"])] = item
    values = list(articles.values())
    if omit_last:
        values = values[:-1]
    payload = {
        "schema_version": 1,
        "annotation_method": "subagent-original-content-v1",
        "source_scope": "linked-original-content-with-report-fallback",
        "generated_on": "2026-08-18",
        "articles": values,
    }
    (metadata / "articles-001-999.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _registry(sources: Path, root: Path) -> Path:
    database = root / "registry.sqlite3"
    build_audit_registry(sources, database, root / "registry-audit")
    return database


def _environment(tmp_path: Path, dates: tuple[tuple[str, int, int], ...] = (("2026-08-10", 1, 1),)):
    sources = tmp_path / "sources"
    reports = [
        _write_report(sources, report_date, a_count=a_count, b_count=b_count)
        for report_date, a_count, b_count in dates
    ]
    metadata = tmp_path / "article-artifacts"
    _write_annotations(metadata, reports)
    database = _registry(sources, tmp_path)
    return sources, database, metadata, reports


def _run(
    sources: Path,
    database: Path,
    metadata: Path,
    output: Path,
    *,
    report_date: str | None = "2026-08-10",
    all_missing: bool = False,
    dry_run: bool = False,
):
    return backfill_reports(
        sources_dir=sources,
        registry_db=database,
        article_artifacts_dir=metadata,
        output_dir=output,
        report_date=report_date,
        all_missing=all_missing,
        dry_run=dry_run,
    )


def test_single_date_dry_run_is_structured_and_writes_no_output(tmp_path):
    sources, database, metadata, _reports = _environment(tmp_path)
    output = tmp_path / "output"

    result = _run(sources, database, metadata, output, dry_run=True)

    assert result["status"] == "dry-run"
    assert result["counts"] == {"generated": 1, "skipped": 0, "already_valid": 0, "failed": 0}
    assert result["generated"] == [
        {
            "report_date": "2026-08-10",
            "report_sha256": result["generated"][0]["report_sha256"],
            "pillar_a_updates": 1,
            "pillar_b_updates": 1,
            "action": "would_generate",
        }
    ]
    assert not output.exists()


def test_all_missing_dry_run_audits_multiple_pillar_counts_in_date_order(tmp_path):
    dates = (("2026-08-03", 2, 1), ("2026-08-10", 1, 3))
    sources, database, metadata, _reports = _environment(tmp_path, dates)

    result = _run(
        sources,
        database,
        metadata,
        tmp_path / "output",
        report_date=None,
        all_missing=True,
        dry_run=True,
    )

    assert [(item["report_date"], item["pillar_a_updates"], item["pillar_b_updates"]) for item in result["generated"]] == [
        ("2026-08-03", 2, 1),
        ("2026-08-10", 1, 3),
    ]
    assert result["counts"]["generated"] == 2


def test_generates_complete_validated_artifact_from_annotations(tmp_path):
    sources, database, metadata, reports = _environment(tmp_path)
    output = tmp_path / "output"

    result = _run(sources, database, metadata, output)

    generated = result["generated"][0]
    artifact_dir = output / "2026-08-10" / generated["report_sha256"]
    assert generated["action"] == "generated"
    assert sorted(path.name for path in artifact_dir.iterdir()) == [
        "climate-monitor-2026-08-10.pdf",
        "manifest.json",
        "summary.json",
    ]
    summary_raw = (artifact_dir / "summary.json").read_bytes()
    summary = json.loads(summary_raw)
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    pdf_raw = (artifact_dir / "climate-monitor-2026-08-10.pdf").read_bytes()
    assert [item["title"] for item in summary["highlights"]] == [
        "Canonical Source A1",
        "Canonical Source B1",
    ]
    assert summary["report"]["sites"] == {"checked": 8, "succeeded": 7, "failed": 1}
    assert summary["monitoring_notes"] == [
        "Monitoring remained focused on source-backed actuarial developments."
    ]
    assert manifest["delivery"] == {"status": "artifact-only", "recipients": []}
    assert manifest["artifacts"]["summary"]["sha256"] == hashlib.sha256(summary_raw).hexdigest()
    assert manifest["artifacts"]["pdf"]["sha256"] == hashlib.sha256(pdf_raw).hexdigest()
    assert pdf_raw.startswith(b"%PDF-")

    from climate_delivery.report import parse_weekly_report

    report = parse_weekly_report(reports[0])
    artifact = load_report_artifact(
        output,
        report_date=report.report_date,
        report_filename=report.filename,
        report_title=report.title,
        report_sha256=report.sha256,
        include_pdf_bytes=True,
    )
    assert artifact is not None
    assert artifact.briefing["monitoring_snapshot"]["pillar_a_updates"] == 1
    assert artifact.briefing["monitoring_snapshot"]["pillar_b_updates"] == 1
    assert artifact.pdf_bytes == pdf_raw


def test_generated_artifact_is_projected_and_downloaded_by_registry_api(
    tmp_path, monkeypatch
):
    sources, database, metadata, _reports = _environment(tmp_path)
    output = tmp_path / "output"
    result = _run(sources, database, metadata, output)

    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setenv("CLIMATE_DELIVERY_OUTPUT_DIR", str(output))
    monkeypatch.setattr(api_server, "SOURCE_DIR", sources)
    monkeypatch.setattr(api_server, "ARTICLE_METADATA_DIR", metadata)
    client = TestClient(api_server.app)

    detail = client.get("/api/registry/reports/2026-08-10")
    assert detail.status_code == 200
    assert detail.json()["report_briefing"]["monitoring_snapshot"] == {
        "sites_checked": 8,
        "sites_succeeded": 7,
        "sites_failed": 1,
        "pillar_a_updates": 1,
        "pillar_b_updates": 1,
        "notes": [
            "Monitoring remained focused on source-backed actuarial developments."
        ],
    }
    assert detail.json()["report_pdf"]["download_url"] == (
        "/api/registry/reports/2026-08-10/pdf"
    )
    download = client.get(detail.json()["report_pdf"]["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    assert download.content == (
        output
        / "2026-08-10"
        / result["generated"][0]["report_sha256"]
        / "climate-monitor-2026-08-10.pdf"
    ).read_bytes()


def test_missing_markdown_and_missing_registry_membership_are_explicitly_skipped(tmp_path):
    sources, database, metadata, reports = _environment(tmp_path)
    reports[0].unlink()
    missing_markdown = _run(sources, database, metadata, tmp_path / "output")
    assert missing_markdown["skipped"] == [
        {"report_date": "2026-08-10", "reason": "missing_markdown"}
    ]

    source_only = tmp_path / "source-only"
    report = _write_report(source_only, "2026-08-03")
    metadata_only = tmp_path / "source-only-artifacts"
    _write_annotations(metadata_only, [report])
    missing_registry = _run(
        source_only,
        database,
        metadata_only,
        tmp_path / "output-two",
        report_date="2026-08-03",
    )
    assert missing_registry["skipped"] == [
        {"report_date": "2026-08-03", "reason": "missing_registry_membership"}
    ]


def test_incomplete_unique_article_artifacts_skip_without_partial_output(tmp_path):
    sources, database, metadata, reports = _environment(tmp_path)
    _write_annotations(metadata, reports, omit_last=True)
    output = tmp_path / "output"

    result = _run(sources, database, metadata, output)

    assert result["skipped"][0]["reason"] == "incomplete_article_artifacts"
    assert result["skipped"][0]["evidence"] == {"expected": 2, "matched": 1}
    assert not output.exists()


def test_missing_monitoring_statistics_and_legacy_reports_skip(tmp_path):
    sources, database, metadata, reports = _environment(tmp_path)
    reports[0].write_text(
        _weekly(
            "2026-08-10",
            a_items=(("Source A1", "Source summary A1.", "https://a1.example.test/2026-08-10"),),
            b_items=(("Source B1", "Source summary B1.", "https://b1.example.test/2026-08-10"),),
            include_monitoring=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    missing_stats = _run(sources, database, metadata, tmp_path / "output")
    assert missing_stats["skipped"][0]["reason"] == "missing_monitoring_statistics"

    legacy_sources = tmp_path / "legacy-sources"
    legacy_sources.mkdir()
    legacy = legacy_sources / "climate-monitor-2026-08-03.md"
    legacy.write_text(
        "# Climate Monitor 2026-08-03\n## Executive Summary\n- Legacy.\n"
        "## Part 1 Website Updates\n**Legacy item**\nSummary text.\n"
        "https://legacy.example.test/item\n## Original Links\n- https://legacy.example.test/item\n",
        encoding="utf-8",
    )
    legacy_registry = _registry(legacy_sources, tmp_path / "legacy-runtime")
    legacy_metadata = tmp_path / "legacy-artifacts"
    legacy_metadata.mkdir()
    (legacy_metadata / "articles-001-001.json").write_text("{}", encoding="utf-8")
    legacy_result = _run(
        legacy_sources,
        legacy_registry,
        legacy_metadata,
        tmp_path / "legacy-output",
        report_date="2026-08-03",
    )
    assert legacy_result["skipped"][0]["reason"] == "legacy_report_incomplete_for_backfill"


def test_duplicate_or_ambiguous_source_to_registry_mapping_skips(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    report = sources / "climate-monitor-2026-08-10.md"
    duplicate_url = "https://duplicate.example.test/item"
    report.write_text(
        _weekly(
            "2026-08-10",
            a_items=(("First", "First summary.", duplicate_url),),
            b_items=(("Second", "Second summary.", duplicate_url),),
        ),
        encoding="utf-8",
        newline="\n",
    )
    metadata = tmp_path / "article-artifacts"
    _write_annotations(metadata, [report])
    database = _registry(sources, tmp_path)

    result = _run(sources, database, metadata, tmp_path / "output")

    assert result["skipped"][0]["reason"] == "duplicate_or_ambiguous_article_mapping"
    assert not (tmp_path / "output").exists()


def test_registry_discovery_conflict_skips_instead_of_guessing_membership(tmp_path):
    sources, database, metadata, _reports = _environment(tmp_path)
    connection = sqlite3.connect(database)
    try:
        with connection:
            connection.execute(
                "UPDATE discoveries SET raw_url = ? WHERE ordinal = 1",
                ("https://conflict.example.test/item",),
            )
    finally:
        connection.close()

    result = _run(sources, database, metadata, tmp_path / "output")

    assert result["skipped"][0]["reason"] == "registry_membership_conflict"
    assert result["skipped"][0]["evidence"] == {
        "ordinal": 1,
        "conflicting_fields": ["raw_url"],
    }
    assert not (tmp_path / "output").exists()


def test_registry_cross_table_ownership_conflict_fails_closed(tmp_path):
    sources, database, metadata, _reports = _environment(tmp_path)
    connection = sqlite3.connect(database)
    try:
        with connection:
            article_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT article_id FROM articles ORDER BY article_id"
                )
            ]
            connection.execute(
                "UPDATE discoveries SET article_id = ? WHERE ordinal = 1",
                (article_ids[1],),
            )
    finally:
        connection.close()

    with pytest.raises(InputError, match="unsupported schema"):
        _run(sources, database, metadata, tmp_path / "output")

    assert not (tmp_path / "output").exists()


def test_source_sha_identity_mismatch_is_reported_and_not_generated(tmp_path):
    sources, database, metadata, reports = _environment(tmp_path)
    reports[0].write_text(
        reports[0].read_text(encoding="utf-8").replace("source-backed", "verified source-backed"),
        encoding="utf-8",
        newline="\n",
    )

    result = _run(sources, database, metadata, tmp_path / "output")

    assert result["skipped"][0]["reason"] == "source_sha_mismatch"
    assert set(result["skipped"][0]["evidence"]) == {"registry_sha256", "source_sha256"}
    assert not (tmp_path / "output").exists()


def test_registry_monitoring_conflict_is_reported_with_evidence(tmp_path):
    sources, database, metadata, _reports = _environment(tmp_path)
    connection = sqlite3.connect(database)
    try:
        with connection:
            connection.execute(
                """
                UPDATE reports
                SET sites_checked = 9, sites_succeeded = 8, sites_failed = 1
                WHERE report_date = '2026-08-10'
                """
            )
    finally:
        connection.close()

    result = _run(sources, database, metadata, tmp_path / "output")

    assert result["skipped"] == [
        {
            "report_date": "2026-08-10",
            "reason": "monitoring_statistics_conflict",
            "evidence": {
                "registry": {"checked": 9, "succeeded": 8, "failed": 1},
                "source": {"checked": 8, "succeeded": 7, "failed": 1},
            },
        }
    ]


def test_valid_artifact_and_2026_08_17_are_never_overwritten(tmp_path):
    sources, database, metadata, _reports = _environment(tmp_path, (("2026-08-17", 2, 3),))
    output = tmp_path / "output"

    missing = _run(
        sources,
        database,
        metadata,
        output,
        report_date="2026-08-17",
    )
    assert missing["skipped"] == [
        {
            "report_date": "2026-08-17",
            "reason": "protected_delivery_artifact_unavailable",
        }
    ]
    assert not output.exists()

    source = backfill_module._source_report(sources, "2026-08-17")
    enriched = backfill_module._enriched_report(
        source,
        backfill_module._normalized_source_articles(source),
        load_article_annotations(metadata),
    )
    artifact_dir = backfill_module._write_candidate(output, enriched)
    before = {path.name: path.read_bytes() for path in artifact_dir.iterdir()}

    second = _run(
        sources,
        database,
        metadata,
        output,
        report_date="2026-08-17",
    )

    assert second["already_valid"] == [
        {
            "report_date": "2026-08-17",
            "report_sha256": source.sha256,
        }
    ]
    assert {path.name: path.read_bytes() for path in artifact_dir.iterdir()} == before


def test_valid_artifact_is_already_valid_even_when_source_is_unavailable(tmp_path):
    sources, database, metadata, reports = _environment(tmp_path)
    output = tmp_path / "output"
    first = _run(sources, database, metadata, output)
    digest = first["generated"][0]["report_sha256"]
    artifact_dir = output / "2026-08-10" / digest
    before = {path.name: path.read_bytes() for path in artifact_dir.iterdir()}
    reports[0].unlink()

    second = _run(sources, database, metadata, output)

    assert second["already_valid"] == [
        {"report_date": "2026-08-10", "report_sha256": digest}
    ]
    assert second["skipped"] == []
    assert {path.name: path.read_bytes() for path in artifact_dir.iterdir()} == before


def test_repository_read_only_input_mounts_are_allowed(tmp_path):
    _sources, database, _metadata, _reports = _environment(tmp_path)
    repository = Path(__file__).resolve().parents[1]

    sources, registry, metadata, output = backfill_module._validate_paths(
        repository / "sources",
        database,
        repository / "article_metadata",
        tmp_path / "output",
    )

    assert sources == (repository / "sources").resolve()
    assert metadata == (repository / "article_metadata").resolve()
    assert registry == database.resolve()
    assert output == (tmp_path / "output").resolve()


def test_registry_database_file_symlink_is_rejected(tmp_path):
    sources, database, metadata, _reports = _environment(tmp_path)
    registry_link = tmp_path / "registry-link.sqlite3"
    try:
        registry_link.symlink_to(database)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(InputError, match="symbolic link"):
        _run(
            sources,
            registry_link,
            metadata,
            tmp_path / "output",
        )


def test_repeated_clean_generation_is_byte_deterministic(tmp_path):
    sources, database, metadata, _reports = _environment(tmp_path)
    first_output = tmp_path / "first-output"
    second_output = tmp_path / "second-output"

    first = _run(sources, database, metadata, first_output)
    second = _run(sources, database, metadata, second_output)
    digest = first["generated"][0]["report_sha256"]
    assert second["generated"][0]["report_sha256"] == digest
    first_dir = first_output / "2026-08-10" / digest
    second_dir = second_output / "2026-08-10" / digest
    assert {
        path.name: path.read_bytes() for path in first_dir.iterdir()
    } == {
        path.name: path.read_bytes() for path in second_dir.iterdir()
    }


def test_ambiguous_duplicate_annotation_artifacts_fail_closed(tmp_path):
    sources, database, metadata, _reports = _environment(tmp_path)
    payload = json.loads(
        (metadata / "articles-001-999.json").read_text(encoding="utf-8")
    )
    payload["articles"] = [payload["articles"][0]]
    (metadata / "articles-duplicate.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    result = _run(sources, database, metadata, tmp_path / "output")

    assert result["skipped"] == [
        {
            "report_date": "2026-08-10",
            "reason": "invalid_or_ambiguous_article_artifacts",
            "evidence": {"batch_files": 2},
        }
    ]
    assert not (tmp_path / "output").exists()


def test_invalid_existing_artifact_directory_fails_without_overwrite(tmp_path):
    sources, database, metadata, reports = _environment(tmp_path)
    digest = hashlib.sha256(reports[0].read_bytes()).hexdigest()
    artifact_dir = tmp_path / "output" / "2026-08-10" / digest
    artifact_dir.mkdir(parents=True)
    marker = artifact_dir / "keep.txt"
    marker.write_bytes(b"do not replace")

    result = _run(sources, database, metadata, tmp_path / "output")

    assert result["failed"] == [
        {
            "report_date": "2026-08-10",
            "report_sha256": digest,
            "reason": "invalid_existing_artifact",
        }
    ]
    assert marker.read_bytes() == b"do not replace"


def test_publish_failure_and_render_failure_leave_no_partial_artifacts(tmp_path, monkeypatch):
    sources, database, metadata, reports = _environment(tmp_path)
    output = tmp_path / "output"
    digest = hashlib.sha256(reports[0].read_bytes()).hexdigest()

    def publish_failure(*_args, **_kwargs):
        raise GenerationError("simulated atomic publish failure")

    monkeypatch.setattr(backfill_module, "_publish_candidate", publish_failure)
    publish_result = _run(sources, database, metadata, output)
    assert publish_result["failed"][0]["reason"] == "artifact_generation_failed"
    assert not (output / "2026-08-10" / digest).exists()
    assert not list(output.glob(".backfill-*"))

    monkeypatch.undo()

    def render_failure(*_args, **_kwargs):
        raise GenerationError("simulated render failure")

    monkeypatch.setattr(backfill_module, "render_pdf", render_failure)
    render_result = _run(sources, database, metadata, output)
    assert render_result["failed"][0]["reason"] == "artifact_generation_failed"
    assert not (output / "2026-08-10" / digest).exists()
    assert not list(output.glob(".backfill-*"))


def test_post_rename_durability_failure_rolls_publish_back(tmp_path, monkeypatch):
    output = tmp_path / "output"
    candidate = output / ".backfill-test" / "candidate"
    candidate.mkdir(parents=True)
    marker = candidate / "complete.txt"
    marker.write_text("complete", encoding="utf-8")
    destination = output / "2026-08-10" / ("a" * 64)
    calls = 0

    if backfill_module.os.name == "posix":
        original_fsync = backfill_module.os.fsync

        def fail_second_fsync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated directory fsync failure")
            return original_fsync(descriptor)

        monkeypatch.setattr(backfill_module.os, "fsync", fail_second_fsync)
    else:
        original_fsync_parent = backfill_module._fsync_parent

        def fail_second_parent(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated directory fsync failure")
            return original_fsync_parent(path)

        monkeypatch.setattr(backfill_module, "_fsync_parent", fail_second_parent)

    with pytest.raises(GenerationError, match="durably publish"):
        backfill_module._publish_candidate(candidate, destination)

    assert not destination.exists()
    assert marker.read_text(encoding="utf-8") == "complete"


def test_publish_lock_is_not_removed_when_another_run_owns_it(tmp_path):
    output = tmp_path / "output"
    candidate = output / ".backfill-test" / "candidate"
    candidate.mkdir(parents=True)
    destination = output / "2026-08-10" / ("a" * 64)
    destination.parent.mkdir(parents=True)
    lock = destination.parent / f".{destination.name}.backfill.lock"
    lock.write_text("other run", encoding="utf-8")

    with pytest.raises(GenerationError, match="atomically publish"):
        backfill_module._publish_candidate(candidate, destination)

    assert lock.read_text(encoding="utf-8") == "other run"
    assert candidate.is_dir()


def test_existing_date_symlink_cannot_redirect_publish(tmp_path):
    sources, database, metadata, _reports = _environment(tmp_path)
    output = tmp_path / "output"
    outside = tmp_path / "outside"
    output.mkdir()
    outside.mkdir()
    try:
        (output / "2026-08-10").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    result = _run(sources, database, metadata, output)

    assert result["failed"][0]["reason"] == "artifact_generation_failed"
    assert not list(outside.iterdir())
    assert not list(output.glob(".backfill-*"))


def test_cli_backfill_has_no_delivery_config_email_or_state_access(tmp_path, monkeypatch, capsys):
    sources, database, metadata, _reports = _environment(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("delivery side effect was accessed")

    monkeypatch.setattr("climate_delivery.cli.load_delivery_config", forbidden)
    monkeypatch.setattr("climate_delivery.cli.deliver", forbidden)
    monkeypatch.setattr("climate_delivery.cli.run_delivery", forbidden)
    code = main(
        [
            "backfill",
            "--date",
            "2026-08-10",
            "--sources-dir",
            str(sources),
            "--registry-db",
            str(database),
            "--article-artifacts-dir",
            str(metadata),
            "--output-dir",
            str(tmp_path / "output"),
            "--dry-run",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "dry-run"
    assert "config" not in payload
    assert "state" not in payload
    assert "recipients" not in payload


@pytest.mark.parametrize(
    "selector",
    [[], ["--date", "2026-08-10", "--all-missing"]],
)
def test_cli_requires_exactly_one_date_selector(tmp_path, selector, capsys):
    sources, database, metadata, _reports = _environment(tmp_path)
    code = main(
        [
            "backfill",
            *selector,
            "--sources-dir",
            str(sources),
            "--registry-db",
            str(database),
            "--article-artifacts-dir",
            str(metadata),
            "--output-dir",
            str(tmp_path / "output"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["kind"] == "input"
