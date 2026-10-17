"""Exact approved-snapshot category corrections; all other evidence is retained."""
from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

from climate_monitor.taxonomy import frozen_article_taxonomy, _semantic_values
from .acquisition_review import digest, now_stamp
from .errors import RegistryInputError
from .persistent import _backup_connection, _backup_name, _exclusive_database_lock
from .publication import resolve_database, require_publication_migration
from .read_api import RegistryReader


def correct_article_categories(*, database, manifest, apply=False, backup_dir=None):
    try:
        database = resolve_database(database)
        plan = json.loads(Path(manifest).read_text(encoding="utf-8"))
        if plan.get("schema_version") != "registry-category-correction.v1":
            raise ValueError("unsupported category correction manifest")
        taxonomy = frozen_article_taxonomy(plan.get("taxonomy"))
        records = plan.get("records")
        if not isinstance(records, list) or not records:
            raise ValueError("category correction records must be a non-empty list")
        keys = set()
        for item in records:
            key = (item["entity_kind"], item["entity_id"])
            if key[0] not in {"article", "pdf_article"} or key in keys:
                raise ValueError("category correction identities must be unique articles")
            keys.add(key)
            _semantic_values(item["categories"], name="categories",
                minimum=taxonomy.constraints.categories_min_items, maximum=taxonomy.constraints.categories_max_items,
                item_maximum=None, allowed=taxonomy.allowed_labels)
            if (not isinstance(item.get("evidence_basis"), str) or not item["evidence_basis"].strip()
                    or not isinstance(item.get("canonical_url"), str)
                    or not isinstance(item.get("expected_categories"), list)
                    or set(item["expected_binding"]) != {"is_visible", "published_candidate_sha256", "latest_candidate_sha256"}
                    or item["expected_binding"]["is_visible"] != 1):
                raise ValueError("category correction evidence or exact public binding is missing")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RegistryInputError(str(exc)) from exc
    if apply and backup_dir is None:
        raise RegistryInputError("--apply requires --backup-dir")
    operation_sha = digest(plan)
    source_ref = "category-correction:" + operation_sha
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1], public=False)
    lock = _exclusive_database_lock(database) if apply else nullcontext()
    backup = None
    with lock:
        connection = sqlite3.connect(database if apply else f"{database.as_uri()}?mode=ro", uri=not apply)
        connection.row_factory = sqlite3.Row
        try:
            if apply:
                connection.execute("PRAGMA foreign_keys=ON")
            reader._validate_contract(connection)
            require_publication_migration(connection)
            if apply:
                backup_root = Path(backup_dir).expanduser().resolve()
                backup_root.mkdir(parents=True, exist_ok=True)
                backup = backup_root / _backup_name(database)
                _backup_connection(connection, backup)
            connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            changed = 0
            repeated = 0
            stamp = now_stamp()
            with reader.public_snapshot(direct=True, source_connection=connection):
                for item in records:
                    key = (item["entity_kind"], item["entity_id"])
                    previous = item["expected_binding"]
                    row = connection.execute("SELECT snapshot_json,entity_kind,entity_id FROM registry_candidates WHERE candidate_sha256=?",
                        (previous["published_candidate_sha256"],)).fetchone()
                    approved = connection.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=? AND status IN ('pass','accepted_legacy')",
                        (previous["published_candidate_sha256"],)).fetchone()
                    if row is None or (row[1], row[2]) != key or not approved:
                        raise RegistryInputError("category correction has no exact approved snapshot")
                    original = json.loads(row[0])
                    if digest(original) != previous["published_candidate_sha256"]:
                        raise RegistryInputError("category correction approved snapshot hash differs")
                    payload = (reader._article_card_from_snapshot(original) if key[0] == "article"
                        else reader._pdf_article_from_snapshot(original))
                    if payload["canonical_url"] != item["canonical_url"] or (payload.get("categories") or []) != item["expected_categories"]:
                        raise RegistryInputError("category correction original URL or categories differ")
                    snapshot = deepcopy(original)
                    display = dict(snapshot.get("derived_display") or {})
                    display["categories"] = item["categories"]
                    display["metadata_provenance"] = {**(payload.get("metadata_provenance") or {}),
                        "categories": "reviewed_category_correction"}
                    if key[0] == "pdf_article" and payload.get("manual_enrichment") is not None:
                        display["manual_enrichment"] = {**payload["manual_enrichment"], "categories": item["categories"]}
                    snapshot["derived_display"] = display
                    candidate_sha = digest(snapshot)
                    current = connection.execute("SELECT is_visible,published_candidate_sha256,latest_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", key).fetchone()
                    next_latest = candidate_sha if previous["latest_candidate_sha256"] == previous["published_candidate_sha256"] else previous["latest_candidate_sha256"]
                    if current is not None and tuple(current) == (1, candidate_sha, next_latest):
                        saved = connection.execute("SELECT 1 FROM registry_candidates c JOIN registry_reviews r ON c.candidate_sha256=r.candidate_sha256 WHERE c.candidate_sha256=? AND c.basis=? AND r.status='pass'", (candidate_sha, source_ref)).fetchone()
                        if not saved:
                            raise RegistryInputError("category correction repeat has no matching review receipt")
                        repeated += 1
                        continue
                    if current is None or tuple(current) != (1, previous["published_candidate_sha256"], previous["latest_candidate_sha256"]):
                        raise RegistryInputError("category correction public, pending or visibility binding is stale")
                    changed += 1
                    if not apply:
                        continue
                    receipt = {"schema_version": "registry-category-correction-review.v1", "scope": "approved-public-category-only",
                        "manifest_sha256": operation_sha, "taxonomy": plan["taxonomy"], "previous_candidate_sha256": previous["published_candidate_sha256"],
                        "evidence_basis": item["evidence_basis"], "evidence_refs": item.get("evidence_refs", {})}
                    connection.execute("INSERT INTO registry_candidates VALUES(?,?,?,?,?,?)", (candidate_sha, *key,
                        json.dumps(snapshot, sort_keys=True, ensure_ascii=False), stamp, source_ref))
                    connection.execute("INSERT INTO registry_reviews VALUES(?,?,?,?,?)", (digest([candidate_sha, receipt]), candidate_sha,
                        "pass", stamp, json.dumps(receipt, sort_keys=True, ensure_ascii=False)))
                    cursor = connection.execute("""UPDATE registry_publication SET published_candidate_sha256=?,latest_candidate_sha256=?
                        WHERE entity_kind=? AND entity_id=? AND is_visible=1 AND published_candidate_sha256=? AND latest_candidate_sha256=?""",
                        (candidate_sha, next_latest, *key, previous["published_candidate_sha256"], previous["latest_candidate_sha256"]))
                    if cursor.rowcount != 1:
                        raise RegistryInputError("category correction publication CAS failed")
                    evidence = {**receipt, "entity_kind": key[0], "old_binding": previous, "new_candidate_sha256": candidate_sha,
                        "categories_before": item["expected_categories"], "categories_after": item["categories"]}
                    fields = {"categories": item["categories"]}
                    connection.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("category-" + digest([source_ref, key]),
                        "article", key[1], "pdf" if key[0] == "pdf_article" else "site", source_ref, digest(fields),
                        json.dumps(fields, ensure_ascii=False), json.dumps(evidence, sort_keys=True, ensure_ascii=False),
                        None, None, stamp, "Reviewed category-only correction; source content and dates retained"))
            if apply:
                from .display import refresh_display
                reader._validate_contract(connection)
                refresh_display(connection)
                connection.commit()
            else:
                connection.rollback()
            return {"status": "already_applied" if repeated == len(records) else "applied" if apply else "dry_run",
                "manifest_sha256": operation_sha, "changed": changed, "already_applied": repeated,
                "backup": str(backup) if backup else None, "pending_records": len(plan.get("pending_records", []))}
        except (OSError, ValueError, sqlite3.Error) as exc:
            connection.rollback()
            raise RegistryInputError(str(exc)) from exc
        finally:
            connection.close()
