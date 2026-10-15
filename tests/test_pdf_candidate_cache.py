import sqlite3

import pytest

from climate_registry.publication import published_snapshots
from test_issue221_review_repairs import _approve_all
from test_registry_publication import _database


def test_cached_candidate_query_omits_large_json_but_checks_current_binding(tmp_path):
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        _approve_all(db)
        cache = {}
        first = published_snapshots(db, ("article",), candidate_cache=cache)
        returned_json = []
        class ObservedConnection:
            def execute(self, sql, parameters=()):
                for row in db.execute(sql, parameters):
                    if "snapshot_json" in sql:
                        returned_json.append(row[7])
                    yield row
        second = published_snapshots(ObservedConnection(), ("article",), candidate_cache=cache)
        assert second == first
        assert returned_json == [None, None]
        db.execute("DROP TRIGGER registry_publication_requires_approval_update")
        db.execute("UPDATE registry_publication SET published_candidate_sha256='broken' WHERE entity_id='a'")
        with pytest.raises(ValueError, match="binding differs"):
            published_snapshots(ObservedConnection(), ("article",), candidate_cache=cache)
