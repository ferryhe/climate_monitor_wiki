"""PR-D: review-gated, SHA-bound semantic import into the Registry.

These tests pin the contract of ``climate_registry.semantic_import`` and its
``semantic-import`` CLI subcommand:

* **Review-gated.** The CLI default is a dry-run that writes nothing; a human
  must pass ``--apply`` to mutate the database.
* **SHA-bound.** Every call requires the *exact* canonical report sha256. A
  missing / malformed / wrong SHA raises ``RegistryInputError`` and never
  writes.
* **Fail-closed.** A missing or tampered sidecar propagates
  ``SemanticBundleError`` from ``verify_semantic_sidecar``; nothing is written.
* **Deterministic.** The dry-run plan is byte-identical across repeated calls.

Everything is offline: the sidecar fixture is built in-process from the repo's
committed taxonomy, there is no network and no LLM call on this path.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from climate_monitor.semantic_bundle import (
    SemanticBundleError,
    semantic_sidecar_path,
    serialize_sidecar,
)
from climate_registry.cli import main
from climate_registry.errors import RegistryInputError
from climate_registry.semantic_import import (
    _build_records,
    _target_article_identities,
    import_report_semantics,
)

from test_climate_delivery_pipeline import DELIVERY_REPORT, _write_sidecar


REPORT_NAME = "climate-monitor-2026-08-10.md"


# ---------------------------------------------------------------------------
# Offline fixtures
# ---------------------------------------------------------------------------


def _report_with_sidecar(tmp_path: Path, name: str = REPORT_NAME) -> Path:
    """Write a canonical Markdown report plus its verified semantic sidecar."""

    path = tmp_path / name
    path.write_text(DELIVERY_REPORT, encoding="utf-8")
    _write_sidecar(path)
    return path


def _report_sha256(report: Path) -> str:
    return hashlib.sha256(report.read_bytes()).hexdigest()


def _empty_database(tmp_path: Path, name: str = "registry.sqlite3") -> Path:
    """An existing but empty sqlite database; migrations run on apply."""

    database = tmp_path / name
    sqlite3.connect(database).close()
    return database


def _semantics_rows(database: Path) -> list[tuple]:
    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "article_semantics" not in tables:
            return []
        return list(
            connection.execute(
                """
                SELECT report_sha256, article_id, canonical_url, title, summary,
                       categories_json, keywords_json, taxonomy_id,
                       taxonomy_raw_sha256, bundle_sha256
                FROM article_semantics ORDER BY article_id
                """
            )
        )
    finally:
        connection.close()


def _database_digest(database: Path) -> str:
    return hashlib.sha256(database.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Fixture sanity: the offline sidecar really is verifiable and joins 1:1.
# ---------------------------------------------------------------------------


def test_fixture_report_and_sidecar_join_one_to_one(tmp_path):
    report = _report_with_sidecar(tmp_path)
    assert semantic_sidecar_path(report).is_file()
    identities = _target_article_identities(report)
    assert len(identities) == 2


# ---------------------------------------------------------------------------
# Dry-run: deterministic plan, writes nothing.
# ---------------------------------------------------------------------------


def test_dry_run_returns_plan_and_writes_nothing(tmp_path):
    report = _report_with_sidecar(tmp_path)
    database = _empty_database(tmp_path)
    before = _database_digest(database)

    plan = import_report_semantics(
        report,
        expected_report_sha256=_report_sha256(report),
        dry_run=True,
        database=database,
        backup_dir=tmp_path / "backups",
    )

    assert plan["status"] == "dry-run"
    assert plan["report_sha256"] == _report_sha256(report)
    assert plan["would_write"] == 2
    assert plan["would_write"] == len(plan["matched"])
    assert plan["unmatched"] == []
    assert "written" not in plan

    # Nothing at all was touched: no rows, no schema drift, no backup file.
    assert _semantics_rows(database) == []
    assert _database_digest(database) == before
    assert not (tmp_path / "backups").exists()


def test_dry_run_plan_is_deterministic_across_repeated_calls(tmp_path):
    report = _report_with_sidecar(tmp_path)
    sha256 = _report_sha256(report)

    plans = [
        import_report_semantics(report, expected_report_sha256=sha256, dry_run=True)
        for _ in range(12)
    ]
    rendered = {json.dumps(plan, sort_keys=True, default=list) for plan in plans}
    assert len(rendered) == 1
    assert plans[0]["would_write"] == 2


# ---------------------------------------------------------------------------
# Apply: matched rows written 1:1, backup taken.
# ---------------------------------------------------------------------------


def test_apply_writes_matched_rows_one_to_one(tmp_path):
    report = _report_with_sidecar(tmp_path)
    database = _empty_database(tmp_path)
    backup_dir = tmp_path / "backups"
    sha256 = _report_sha256(report)

    plan = import_report_semantics(
        report,
        expected_report_sha256=sha256,
        dry_run=False,
        database=database,
        backup_dir=backup_dir,
    )

    assert plan["status"] == "applied"
    assert plan["written"] == 2
    rows = _semantics_rows(database)
    assert len(rows) == 2
    assert {row[0] for row in rows} == {sha256}
    assert [row[1] for row in rows] == sorted(row[1] for row in rows)
    titles = {row[3] for row in rows}
    assert titles == {"First finding", "Second finding"}
    summaries = {row[4] for row in rows}
    assert summaries == {
        "First article semantic summary.",
        "Second article semantic summary.",
    }
    for row in rows:
        assert json.loads(row[5])  # categories_json
        assert json.loads(row[6])  # keywords_json
        assert row[7]  # taxonomy_id
        assert len(row[8]) == 64  # taxonomy_raw_sha256
        assert len(row[9]) == 64  # bundle_sha256

    # An exact pre-apply backup was retained for the reviewer.
    assert backup_dir.is_dir()
    assert len(list(backup_dir.iterdir())) == 1


def test_apply_is_idempotent_for_the_same_report(tmp_path):
    report = _report_with_sidecar(tmp_path)
    database = _empty_database(tmp_path)
    sha256 = _report_sha256(report)

    import_report_semantics(
        report,
        expected_report_sha256=sha256,
        dry_run=False,
        database=database,
        backup_dir=tmp_path / "backups-one",
    )
    first = _semantics_rows(database)
    import_report_semantics(
        report,
        expected_report_sha256=sha256,
        dry_run=False,
        database=database,
        backup_dir=tmp_path / "backups-two",
    )
    second = _semantics_rows(database)

    assert len(second) == 2
    assert [row[:4] for row in first] == [row[:4] for row in second]


# ---------------------------------------------------------------------------
# The SHA-gate: no exact report SHA, no import.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "supplied",
    [None, "", "not-a-sha", "0" * 64, "A" * 64, "abc"],
)
def test_import_requires_the_exact_report_sha256(tmp_path, supplied):
    report = _report_with_sidecar(tmp_path)
    database = _empty_database(tmp_path)
    before = _database_digest(database)

    with pytest.raises(RegistryInputError, match="exact report SHA"):
        import_report_semantics(
            report,
            expected_report_sha256=supplied,
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )

    assert _semantics_rows(database) == []
    assert _database_digest(database) == before
    assert not (tmp_path / "backups").exists()


def test_sha_gate_applies_to_dry_run_too(tmp_path):
    report = _report_with_sidecar(tmp_path)
    with pytest.raises(RegistryInputError, match="exact report SHA"):
        import_report_semantics(report, expected_report_sha256=None, dry_run=True)


def test_sha_of_a_different_report_is_rejected(tmp_path):
    report = _report_with_sidecar(tmp_path)
    other = tmp_path / "other.md"
    other.write_text(DELIVERY_REPORT + "\n<!-- different bytes -->\n", encoding="utf-8")

    with pytest.raises(RegistryInputError, match="exact report SHA"):
        import_report_semantics(
            report, expected_report_sha256=_report_sha256(other), dry_run=True
        )


# ---------------------------------------------------------------------------
# Fail-closed sidecar verification.
# ---------------------------------------------------------------------------


def test_missing_sidecar_fails_closed(tmp_path):
    report = tmp_path / REPORT_NAME
    report.write_text(DELIVERY_REPORT, encoding="utf-8")
    database = _empty_database(tmp_path)
    assert not semantic_sidecar_path(report).exists()

    with pytest.raises(SemanticBundleError, match="missing"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )
    assert _semantics_rows(database) == []


def test_tampered_sidecar_semantics_fail_closed(tmp_path):
    report = _report_with_sidecar(tmp_path)
    sidecar = semantic_sidecar_path(report)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["articles"][0]["semantics"]["categories"] = ["Not A Taxonomy Category"]
    sidecar.write_bytes(serialize_sidecar(payload))
    database = _empty_database(tmp_path)

    with pytest.raises(SemanticBundleError, match="contract-valid|taxonomy"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )
    assert _semantics_rows(database) == []


def test_sidecar_bound_to_a_different_report_sha_fails_closed(tmp_path):
    report = _report_with_sidecar(tmp_path)
    sidecar = semantic_sidecar_path(report)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["report"]["sha256"] = "0" * 64
    sidecar.write_bytes(serialize_sidecar(payload))

    with pytest.raises(SemanticBundleError, match="sha256|bound"):
        import_report_semantics(
            report, expected_report_sha256=_report_sha256(report), dry_run=True
        )


def test_sidecar_json_corruption_fails_closed(tmp_path):
    report = _report_with_sidecar(tmp_path)
    semantic_sidecar_path(report).write_bytes(b"{not json")

    with pytest.raises(SemanticBundleError, match="JSON"):
        import_report_semantics(
            report, expected_report_sha256=_report_sha256(report), dry_run=True
        )


# ---------------------------------------------------------------------------
# The join: sidecar articles outside the target report's article set are
# dropped and never invent rows.
# ---------------------------------------------------------------------------


def test_unmatched_sidecar_articles_are_dropped_from_the_write_set(tmp_path):
    report = _report_with_sidecar(tmp_path)
    payload = json.loads(semantic_sidecar_path(report).read_text(encoding="utf-8"))
    identities = _target_article_identities(report)

    # Full target set: everything matches, nothing is dropped.
    matched, unmatched = _build_records(payload, identities)
    assert len(matched) == 2
    assert unmatched == []

    # Shrink the target report's article set: the missing identity moves to
    # ``unmatched`` and is therefore excluded from any write.
    dropped = sorted(identities)[0]
    partial = {key: value for key, value in identities.items() if key != dropped}
    matched, unmatched = _build_records(payload, partial)
    assert [record["article_id"] for record in unmatched] == [dropped]
    assert dropped not in {record["article_id"] for record in matched}
    assert len(matched) == 1

    # An empty target set writes nothing at all.
    matched, unmatched = _build_records(payload, {})
    assert matched == []
    assert len(unmatched) == 2


def test_registry_article_id_is_recomputed_from_the_canonical_url(tmp_path):
    """The sidecar's own article_id is a different identity scheme; the import
    must recompute the Registry identity so rows join to the Registry."""

    report = _report_with_sidecar(tmp_path)
    payload = json.loads(semantic_sidecar_path(report).read_text(encoding="utf-8"))
    identities = _target_article_identities(report)
    matched, _ = _build_records(payload, identities)

    sidecar_ids = {article["article_id"] for article in payload["articles"]}
    registry_ids = {record["article_id"] for record in matched}
    assert registry_ids == set(identities)
    assert registry_ids.isdisjoint(sidecar_ids)


# ---------------------------------------------------------------------------
# CLI wiring: dry-run by default, --apply is the human gate.
# ---------------------------------------------------------------------------


def test_cli_defaults_to_dry_run_and_writes_nothing(tmp_path, capsys):
    report = _report_with_sidecar(tmp_path)
    database = _empty_database(tmp_path)
    before = _database_digest(database)

    code = main(
        [
            "semantic-import",
            "--report",
            str(report),
            "--expected-report-sha256",
            _report_sha256(report),
            "--database",
            str(database),
            "--backup-dir",
            str(tmp_path / "backups"),
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "dry-run"
    assert payload["would_write"] == 2
    assert _semantics_rows(database) == []
    assert _database_digest(database) == before


def test_cli_apply_writes_rows(tmp_path, capsys):
    report = _report_with_sidecar(tmp_path)
    database = _empty_database(tmp_path)

    code = main(
        [
            "semantic-import",
            "--report",
            str(report),
            "--expected-report-sha256",
            _report_sha256(report),
            "--database",
            str(database),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--apply",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "applied"
    assert payload["written"] == 2
    assert len(_semantics_rows(database)) == 2


def test_cli_wrong_sha_reports_sanitized_input_error(tmp_path, capsys):
    report = _report_with_sidecar(tmp_path)
    database = _empty_database(tmp_path)

    code = main(
        [
            "semantic-import",
            "--report",
            str(report),
            "--expected-report-sha256",
            "0" * 64,
            "--database",
            str(database),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--apply",
        ]
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "kind": "input",
        "message": "exact report SHA required to import semantics",
        "status": "error",
    }
    assert _semantics_rows(database) == []


def test_cli_requires_report_and_expected_sha(tmp_path, capsys):
    report = _report_with_sidecar(tmp_path)
    assert main(["semantic-import", "--report", str(report)]) == 2
    assert (
        json.loads(capsys.readouterr().out.strip())["message"]
        == "required CLI arguments are missing"
    )
    assert main(["semantic-import", "--expected-report-sha256", _report_sha256(report)]) == 2
    assert (
        json.loads(capsys.readouterr().out.strip())["message"]
        == "required CLI arguments are missing"
    )


def test_cli_apply_without_a_database_fails_closed(tmp_path, capsys):
    report = _report_with_sidecar(tmp_path)

    code = main(
        [
            "semantic-import",
            "--report",
            str(report),
            "--expected-report-sha256",
            _report_sha256(report),
            "--apply",
        ]
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert payload["kind"] == "input"
    assert "database" in payload["message"]
