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

import climate_registry.semantic_import as semantic_import
import climate_registry.weekly as weekly_helpers
from climate_monitor.semantic_bundle import (
    SemanticBundleError,
    semantic_sidecar_path,
    serialize_sidecar,
)
from climate_registry.audit import build_audit_registry
from climate_registry.cli import main
from climate_registry.errors import RegistryInputError
from climate_registry.semantic_import import (
    _build_records,
    _target_article_identities,
    import_report_semantics,
)
from climate_registry.weekly import WeeklyValidationError

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


def _registry_database_for_report(tmp_path: Path, report: Path) -> Path:
    """Build a real Registry DB that already contains the target report."""

    source_dir = tmp_path / "registry-sources"
    source_dir.mkdir()
    (source_dir / report.name).write_bytes(report.read_bytes())
    database = tmp_path / "registry.sqlite3"
    build_audit_registry(source_dir, database, tmp_path / "registry-audit")
    return database


def _duplicate_sha_database(tmp_path: Path) -> Path:
    database = tmp_path / "duplicate-sha.sqlite3"
    connection = sqlite3.connect(database)
    sha256 = "a" * 64
    with connection:
        from climate_registry.schema import apply_migrations

        apply_migrations(connection)
        connection.execute(
            "INSERT INTO sources VALUES ('s', 'example.com', 'Example', '2026-08-10', '2026-08-17')"
        )
        connection.execute(
            """
            INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen)
            VALUES ('article-shared', 'https://example.com/shared', 's',
                    '2026-08-10', '2026-08-17')
            """
        )
        connection.execute(
            """
            INSERT INTO article_versions VALUES (
                'version-shared', 'article-shared', 'Shared title', 'shared title',
                'Shared summary', 'fingerprint-shared', 'report-title-summary',
                '2026-08-10', '2026-08-17'
            )
            """
        )
        for report_id, report_date, filename, discovery_id in (
            ("report-one", "2026-08-10", "one.md", "discovery-one"),
            ("report-two", "2026-08-17", "two.md", "discovery-two"),
        ):
            connection.execute(
                """
                INSERT INTO reports VALUES (?, ?, ?, ?, ?, 'weekly',
                                            'weekly-pillars-v1', 1, 1, 0, '[]')
                """,
                (report_id, report_date, filename, f"Report {report_id}", sha256),
            )
            connection.execute(
                """
                INSERT INTO discoveries VALUES (?, ?, 1, 'pillar-a', 'A',
                                                'article-shared', 'version-shared',
                                                'https://example.com/shared',
                                                'Shared title', 'Shared summary',
                                                1, NULL)
                """,
                (discovery_id, report_id),
            )
            connection.execute(
                """
                INSERT INTO report_appearances(
                    report_id, article_id, version_id, discovery_id, section,
                    pillar, ordinal, disposition
                ) VALUES (?, 'article-shared', 'version-shared', ?, 'pillar-a',
                          'A', 1, 'new')
                """,
                (report_id, discovery_id),
            )
    connection.close()
    return database


def _semantic_record() -> dict:
    return {
        "article_id": "article-shared",
        "canonical_url": "https://example.com/shared",
        "title": "Shared title",
        "summary": "Shared semantic summary.",
        "categories": ["Physical Risk"],
        "keywords": ["risk"],
        "taxonomy_id": "taxonomy",
        "taxonomy_raw_sha256": "b" * 64,
        "bundle_sha256": "c" * 64,
    }


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
                       taxonomy_raw_sha256, bundle_sha256, report_id
                FROM article_semantics ORDER BY report_id, article_id
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
    database = _registry_database_for_report(tmp_path, report)
    backup_dir = tmp_path / "backups"
    sha256 = _report_sha256(report)
    before = database.read_bytes()

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
    assert {row[10] for row in rows} == {"report-2026-08-10"}
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
    backups = list(backup_dir.iterdir())
    assert len(backups) == 1
    assert backups[0].read_bytes() == before


def test_apply_is_idempotent_for_the_same_report(tmp_path):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
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


def test_candidate_apply_keeps_duplicate_sha_reports_separate(tmp_path):
    database = _duplicate_sha_database(tmp_path)
    sha256 = "a" * 64
    record = _semantic_record()
    connection = sqlite3.connect(database)
    try:
        with connection:
            semantic_import._write_semantic_records(
                connection,
                report_id="report-one",
                report_sha=sha256,
                matched=[record],
            )
    finally:
        connection.close()

    written = semantic_import._apply_candidate(
        database,
        report_id="report-two",
        report_sha=sha256,
        matched=[record],
        sidecar_count=1,
    )

    assert written == 1
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            """
            SELECT report_id, report_sha256, article_id
            FROM article_semantics
            ORDER BY report_id, article_id
            """
        ).fetchall()
    finally:
        connection.close()
    assert rows == [
        ("report-one", sha256, "article-shared"),
        ("report-two", sha256, "article-shared"),
    ]


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


def test_dry_run_marks_unmatched_sidecar_rows_as_blocked(tmp_path, monkeypatch):
    report = _report_with_sidecar(tmp_path)
    sha256 = _report_sha256(report)
    identities = _target_article_identities(report)
    partial = {key: value for index, (key, value) in enumerate(identities.items()) if index == 0}

    monkeypatch.setattr(semantic_import, "_target_article_identities", lambda *_args: partial)

    plan = import_report_semantics(report, expected_report_sha256=sha256, dry_run=True)

    assert plan["status"] == "blocked"
    assert plan["blocked"] is True
    assert plan["would_write"] == 1
    assert plan["sidecar_count"] == 2
    assert plan["unmatched_count"] == 1
    assert "unmatched_sidecar_rows" in plan["blockers"]
    assert "sidecar_match_count_mismatch" in plan["blockers"]


def test_apply_refuses_unmatched_sidecar_rows_before_backup(tmp_path, monkeypatch):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
    before = database.read_bytes()
    identities = _target_article_identities(report)
    partial = {key: value for index, (key, value) in enumerate(identities.items()) if index == 0}

    monkeypatch.setattr(semantic_import, "_target_article_identities", lambda *_args: partial)

    with pytest.raises(RegistryInputError, match="unmatched sidecar"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )

    assert database.read_bytes() == before
    assert _semantics_rows(database) == []
    assert not (tmp_path / "backups").exists()


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


def test_apply_requires_report_membership_in_registry_before_backup(tmp_path):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
    connection = sqlite3.connect(database)
    with connection:
        first_article_id = connection.execute(
            "SELECT article_id FROM report_appearances ORDER BY article_id LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM report_appearances WHERE article_id = ?",
            (first_article_id,),
        )
    connection.close()
    before = database.read_bytes()

    with pytest.raises(RegistryInputError, match="target article membership"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )

    assert database.read_bytes() == before
    assert _semantics_rows(database) == []
    assert not (tmp_path / "backups").exists()


def test_apply_requires_report_sha_in_registry_before_backup(tmp_path):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
    connection = sqlite3.connect(database)
    with connection:
        connection.execute("UPDATE reports SET report_sha256 = ?", ("f" * 64,))
    connection.close()
    before = database.read_bytes()

    with pytest.raises(RegistryInputError, match="exact report SHA"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )

    assert database.read_bytes() == before
    assert _semantics_rows(database) == []
    assert not (tmp_path / "backups").exists()


def test_report_bytes_are_snapshotted_once_for_verify_hash_and_parse(tmp_path, monkeypatch):
    report = _report_with_sidecar(tmp_path)
    original_bytes = report.read_bytes()
    original_sha = hashlib.sha256(original_bytes).hexdigest()
    real_verify = semantic_import.verify_semantic_sidecar

    def replace_after_verify(path, *, report_bytes=None):
        assert report_bytes == original_bytes
        payload = real_verify(path, report_bytes=report_bytes)
        report.write_text(
            DELIVERY_REPORT.replace("First finding", "Changed after verify"),
            encoding="utf-8",
        )
        return payload

    monkeypatch.setattr(semantic_import, "verify_semantic_sidecar", replace_after_verify)

    plan = import_report_semantics(
        report,
        expected_report_sha256=original_sha,
        dry_run=True,
    )

    assert plan["status"] == "dry-run"
    assert plan["report_sha256"] == original_sha
    assert plan["would_write"] == 2


def test_apply_rejects_active_sqlite_sidecars_before_backup(tmp_path):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
    Path(f"{database}-wal").write_bytes(b"active wal")
    before = database.read_bytes()

    with pytest.raises(RegistryInputError, match="SQLite sidecar"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )

    assert database.read_bytes() == before
    assert _semantics_rows(database) == []
    assert not (tmp_path / "backups").exists()


def test_apply_rejects_unsafe_database_symlink_before_backup(tmp_path, monkeypatch):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
    before = database.read_bytes()

    monkeypatch.setattr(
        semantic_import,
        "_is_link_or_reparse",
        lambda path: Path(path) == database,
    )

    with pytest.raises(RegistryInputError, match="unsafe"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )

    assert database.read_bytes() == before
    assert _semantics_rows(database) == []
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("unsafe_role", ["database", "backup"])
def test_apply_rejects_raw_path_components_before_resolve(
    tmp_path, monkeypatch, unsafe_role
):
    report = _report_with_sidecar(tmp_path)
    unsafe_dir = tmp_path / f"unsafe-{unsafe_role}"
    unsafe_dir.mkdir()
    if unsafe_role == "database":
        database = _registry_database_for_report(unsafe_dir, report)
        backup_dir = tmp_path / "backups"
    else:
        database = _registry_database_for_report(tmp_path, report)
        backup_dir = unsafe_dir / "backups"
    before = database.read_bytes()
    real_is_link = weekly_helpers._is_link_or_reparse

    def flagged(path):
        return Path(path) == unsafe_dir or real_is_link(path)

    monkeypatch.setattr(weekly_helpers, "_is_link_or_reparse", flagged)

    with pytest.raises(RegistryInputError, match="path is unsafe"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=backup_dir,
        )

    assert database.read_bytes() == before
    assert _semantics_rows(database) == []
    assert not (tmp_path / "backups").exists()
    assert not (unsafe_dir / "backups").exists()


def test_short_backup_writes_are_completed_and_verified(tmp_path, monkeypatch):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
    before = database.read_bytes()
    real_write = weekly_helpers.os.write

    def short_write(descriptor, data):
        return real_write(descriptor, data[: min(len(data), 7)])

    monkeypatch.setattr(weekly_helpers.os, "write", short_write)

    import_report_semantics(
        report,
        expected_report_sha256=_report_sha256(report),
        dry_run=False,
        database=database,
        backup_dir=tmp_path / "backups",
    )

    backup = next((tmp_path / "backups").iterdir())
    assert backup.read_bytes() == before


def test_apply_failure_before_promotion_leaves_live_database_unchanged(
    tmp_path, monkeypatch
):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
    before = database.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise sqlite3.DatabaseError("injected semantic write failure")

    monkeypatch.setattr(semantic_import, "_write_semantic_records", fail_write)

    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )

    assert database.read_bytes() == before
    assert _semantics_rows(database) == []
    assert not (tmp_path / "backups").exists()


def test_candidate_sidecars_after_apply_block_promotion_and_leave_live_unchanged(
    tmp_path, monkeypatch
):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
    before = database.read_bytes()
    real_apply_candidate = semantic_import._apply_candidate

    def apply_candidate_with_sidecar(candidate, **kwargs):
        written = real_apply_candidate(candidate, **kwargs)
        Path(f"{candidate}-wal").write_bytes(b"uncheckpointed candidate wal")
        return written

    monkeypatch.setattr(
        semantic_import,
        "_apply_candidate",
        apply_candidate_with_sidecar,
    )

    with pytest.raises(RegistryInputError, match="candidate has active SQLite sidecar"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )

    assert database.read_bytes() == before
    assert _semantics_rows(database) == []
    assert not (tmp_path / "backups").exists()
    assert not list(database.parent.glob("*.semantic-candidate*"))


def test_written_count_mismatch_blocks_promotion_and_leaves_live_unchanged(
    tmp_path, monkeypatch
):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
    before = database.read_bytes()

    def short_write_count(*_args, **_kwargs):
        return 1

    monkeypatch.setattr(semantic_import, "_write_semantic_records", short_write_count)

    with pytest.raises(RegistryInputError, match="written count"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )

    assert database.read_bytes() == before
    assert _semantics_rows(database) == []
    assert not (tmp_path / "backups").exists()


def test_post_promotion_validation_failure_restores_exact_live_database(
    tmp_path, monkeypatch
):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
    before = database.read_bytes()
    before_sha = hashlib.sha256(before).hexdigest()

    def reject_promoted_database(_connection):
        raise WeeklyValidationError("injected semantic promotion validation")

    real_promote = semantic_import._promote

    def promote_with_validation_failure(*args, **kwargs):
        monkeypatch.setattr(
            weekly_helpers, "_validate_database", reject_promoted_database
        )
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(semantic_import, "_promote", promote_with_validation_failure)

    with pytest.raises(WeeklyValidationError, match="injected"):
        import_report_semantics(
            report,
            expected_report_sha256=_report_sha256(report),
            dry_run=False,
            database=database,
            backup_dir=tmp_path / "backups",
        )

    assert database.read_bytes() == before
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1
    assert hashlib.sha256(backups[0].read_bytes()).hexdigest() == before_sha


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
    database = _registry_database_for_report(tmp_path, report)

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


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_cli_sidecar_errors_are_structured_json(tmp_path, capsys, mutation):
    report = _report_with_sidecar(tmp_path)
    database = _registry_database_for_report(tmp_path, report)
    if mutation == "missing":
        semantic_sidecar_path(report).unlink()
    else:
        semantic_sidecar_path(report).write_bytes(b"{not json")

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

    output = capsys.readouterr().out
    assert code == 2
    assert output.count("\n") == 1
    payload = json.loads(output)
    assert payload["status"] == "error"
    assert payload["kind"] == "semantic_bundle"
    assert "sidecar" in payload["message"]
