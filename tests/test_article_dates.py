import sqlite3

import pytest

from climate_registry.article_dates import ArticleDateImportError, import_observations
from climate_registry.schema import apply_migrations


def _database(path):
    connection = sqlite3.connect(path)
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO sources VALUES ('s', 'example.org', 'Example', '2026-01-01', '2026-01-01')"
    )
    connection.execute(
        """INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen)
           VALUES ('article-a', 'https://example.org/a', 's', '2026-01-01', '2026-01-01')"""
    )
    connection.commit()
    connection.close()
    return path


def _observation(kind, observed_at, *, record_id="17"):
    return {
        "article_id": "article-a",
        "canonical_url": "https://example.org/a",
        "observation_kind": kind,
        "observed_at": observed_at,
        "evidence": {
            "source_system": "web_listening" if kind == "collection" else "publisher_page",
            "database": "web_listening.db" if kind == "collection" else "original-page",
            "table": "site_snapshots" if kind == "collection" else "article_metadata",
            "record_id": record_id,
            "source_url": "https://example.org/a",
            "match_basis": "snapshot_final_url" if kind == "collection" else "publisher_date_field",
        },
    }


def _payload(*observations):
    return {"schema_version": "article-date-observations.v1", "observations": list(observations)}


def test_importer_normalizes_collection_evidence_and_is_idempotent(tmp_path):
    database = _database(tmp_path / "registry.sqlite3")
    item = _observation("collection", "2026-01-31T23:30:00-08:00")

    assert import_observations(database, _payload(item)) == {"inserted": 1, "already_present": 0}
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM article_date_observations").fetchone() == (0,)

    assert import_observations(
        database, _payload(item), write=True, recorded_at="2026-02-04T12:00:00Z"
    ) == {"inserted": 1, "already_present": 0}
    assert import_observations(database, _payload(item), write=True) == {
        "inserted": 0, "already_present": 1
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT observed_at, recorded_at FROM article_date_observations"
        ).fetchone() == ("2026-02-01T07:30:00Z", "2026-02-04T12:00:00Z")


def test_page_information_keeps_day_precision_and_only_fills_missing_collection(tmp_path):
    database = _database(tmp_path / "registry.sqlite3")
    page_date = _observation("page_information", "2026-01-28")
    assert import_observations(database, _payload(page_date), write=True) == {
        "inserted": 1, "already_present": 0
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT observed_at FROM article_date_observations"
        ).fetchone() == ("2026-01-28",)

    collection = _observation("collection", "2026-01-29T00:00:00Z")
    with pytest.raises(ArticleDateImportError, match="only when no collection evidence"):
        import_observations(database, _payload(collection, page_date))


@pytest.mark.parametrize("observed_at", ["2026-01-28T00:00:00", "2026-1-28", "yesterday"])
def test_importer_rejects_unqualified_or_fabricated_precision(tmp_path, observed_at):
    database = _database(tmp_path / "registry.sqlite3")
    item = _observation(
        "collection" if "T" in observed_at else "page_information", observed_at
    )
    with pytest.raises(ArticleDateImportError):
        import_observations(database, _payload(item))
