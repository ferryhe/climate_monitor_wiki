"""Exact candidates, native final review and one shared public SQL projection.

Business writes remain in the external Registry. The disposable projection is
never saved or treated as a valid durable database contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .acquisition_review import digest, now_stamp


def resolve_database(database=None, *, frozen=None):
    configured = database or os.getenv("CLIMATE_REGISTRY_DB", "").strip()
    if not configured:
        raise ValueError("CLIMATE_REGISTRY_DB is required")
    selected=Path(configured).expanduser()
    if not selected.is_absolute():
        raise ValueError("Registry must be an absolute existing database")
    try:
        path=selected.resolve(strict=True)
    except (OSError,RuntimeError) as exc:
        raise ValueError("Registry must be an absolute existing database") from exc
    if not path.is_file():
        raise ValueError("Registry must be an absolute existing database")
    root = Path(__file__).resolve().parents[1]
    if path == root or root in path.parents:
        raise ValueError("Registry must be outside the application repository")
    if frozen is not None and path != Path(frozen).expanduser().resolve(strict=True):
        raise ValueError("configured Registry differs from the frozen task binding")
    return path


ARTICLE_TABLES = {
    "articles": "article_id", "article_versions": "article_id",
    "article_content_versions": "article_id", "article_fetches": "article_id",
    "article_enrichments": "article_id", "url_aliases": "article_id",
    "discoveries": "article_id", "report_appearances": "article_id",
    "article_semantics": "article_id", "article_capture_resolutions": "article_id",
    "acquisition_items": "article_id", "article_date_observations": "article_id",
}
PDF_TABLES = {"pdf_intake_articles": "article_id", "pdf_intake_article_occurrences": "article_id"}
MEETING_TABLES = {"climate_events": "event_id", "climate_event_versions": "event_id",
    "climate_event_sources": "event_id"}
PDF_MEETING_TABLES = {"pdf_intake_calendar_items": "event_id"}
ENTITY_TABLES = {"article": ARTICLE_TABLES, "pdf_article": PDF_TABLES,
    "meeting": MEETING_TABLES, "pdf_meeting": PDF_MEETING_TABLES}
PROJECTED_TABLES = set().union(*(set(value) for value in ENTITY_TABLES.values())) | {
    "article_check_attempts", "meeting_check_attempts", "knowledge_versions", "pdf_intake_documents", "pdf_intake_document_sources"}


def enabled(connection):
    return connection.execute("PRAGMA user_version").fetchone()[0] >= 21


def require_publication_migration(connection):
    """Ordinary writers must not bypass the explicit legacy publication migration."""
    from .errors import RegistryInputError
    if connection.execute("PRAGMA user_version").fetchone()[0] < 22:
        raise RegistryInputError("Registry writes require schema 22; run migrate-publication --apply before writing")


def canonical_entity(connection, kind, entity_id):
    if kind == "pdf_article":
        row = connection.execute("""SELECT a.article_id FROM pdf_intake_articles p JOIN articles a
            ON a.canonical_url=p.canonical_url OR a.article_id=p.core_article_id
            WHERE p.article_id=? ORDER BY a.article_id LIMIT 1""", (entity_id,)).fetchone()
        if row:
            return "article", row[0]
    if kind == "pdf_meeting":
        row = connection.execute("""SELECT json_extract(packet_json,'$.canonical_event_id') FROM meeting_check_attempts
            WHERE occurrence_id IN (SELECT occurrence_id FROM pdf_intake_calendar_items WHERE event_id=?)
              AND verification_status='verified' AND json_extract(packet_json,'$.canonical_event_id') IS NOT NULL
            ORDER BY checked_at DESC LIMIT 1""", (entity_id,)).fetchone()
        if row and connection.execute("SELECT 1 FROM climate_events WHERE event_id=?", (row[0],)).fetchone():
            return "meeting", row[0]
    return kind, entity_id


def _rows(connection, table, predicate, params, *, preserve_order=False):
    cursor = connection.execute(f"SELECT * FROM {table} WHERE {predicate}" + (" ORDER BY rowid" if preserve_order else ""), params)
    names = [column[0] for column in cursor.description]
    import hashlib
    rows = []
    for row in cursor:
        rows.append({key: {"blob_sha256": hashlib.sha256(value).hexdigest(), "blob_size": len(value)} if isinstance(value, bytes) else value for key, value in zip(names,row)})
    return rows if preserve_order else sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def _information_groups(connection):
    from .information_checks import information_targets
    groups = {}
    for check_kind in ("articles","meetings"):
        for target in information_targets(connection,check_kind):
            key = canonical_entity(connection,target["entity_kind"],target["entity_id"])
            groups.setdefault(key,[]).append({"check_kind":check_kind,**target})
    return groups


def snapshot_entity(connection, kind, entity_id, *, include_pending=True, annotations=None, source_dir=None, report_cache=None, information_groups=None):
    kind, entity_id = canonical_entity(connection, kind, entity_id)
    tables = {}
    for table, column in ENTITY_TABLES[kind].items():
        predicate, params = f"{column}=?", (entity_id,)
        if table == "article_enrichments":
            predicate = "article_id=? OR content_version_id IN (SELECT content_version_id FROM article_content_versions WHERE article_id=?)"
            params = (entity_id, entity_id)
        tables[table] = _rows(connection, table, predicate, params)
    dependencies = {}
    if kind == "meeting":
        dependencies["article_content_versions"] = _rows(connection, "article_content_versions",
            "content_version_id IN (SELECT content_version_id FROM climate_event_sources WHERE event_id=?)", (entity_id,))
        dependencies["meeting_runs"] = _rows(connection, "meeting_runs",
            "meeting_run_id IN (SELECT meeting_run_id FROM climate_event_sources WHERE event_id=?)", (entity_id,))
    if kind == "meeting":
        tables["pdf_intake_calendar_items"] = _rows(connection, "pdf_intake_calendar_items",
            "event_id=? OR occurrence_id IN (SELECT occurrence_id FROM meeting_check_attempts WHERE json_extract(packet_json,'$.canonical_event_id')=?)", (entity_id, entity_id))
        tables["meeting_check_attempts"] = [row for item in tables["pdf_intake_calendar_items"] for row in _rows(connection, "meeting_check_attempts", "occurrence_id=?", (item["occurrence_id"],))]
    if kind in {"pdf_article", "pdf_meeting"}:
        table = "pdf_intake_article_occurrences" if kind == "pdf_article" else "pdf_intake_calendar_items"
        check = "article_check_attempts" if kind == "pdf_article" else "meeting_check_attempts"
        tables[check] = _rows(connection, check,
            f"occurrence_id IN (SELECT occurrence_id FROM {table} WHERE {ENTITY_TABLES[kind][table]}=?)", (entity_id,))
    if kind == "article":
        tables["pdf_intake_articles"] = _rows(connection, "pdf_intake_articles",
            "canonical_url IN (SELECT canonical_url FROM articles WHERE article_id=?) OR core_article_id=?", (entity_id, entity_id))
        pdf_ids = [row["article_id"] for row in tables["pdf_intake_articles"]]
        tables["pdf_intake_article_occurrences"] = [row for key in pdf_ids for row in _rows(connection, "pdf_intake_article_occurrences", "article_id=?", (key,))]
        tables["article_check_attempts"] = [row for key in pdf_ids for row in _rows(connection, "article_check_attempts",
            "occurrence_id IN (SELECT occurrence_id FROM pdf_intake_article_occurrences WHERE article_id=?)", (key,))]
    knowledge_kind = "article" if kind in {"article", "pdf_article"} else "meeting"
    knowledge_ids = {entity_id} | {row["article_id"] for row in tables.get("pdf_intake_articles", [])}
    knowledge = [row for identity in sorted(knowledge_ids) for row in _rows(connection, "knowledge_versions", "entity_kind=? AND entity_id=?", (knowledge_kind, identity),preserve_order=True)]
    tables["knowledge_versions"] = knowledge
    targets = (information_groups if information_groups is not None else _information_groups(connection)).get((kind,entity_id),[])
    for prefix, check_kind in (("article","articles"),("meeting","meetings")):
        ids = {target["occurrence_id"] for target in targets if target["check_kind"]==check_kind}
        if not ids:
            continue
        from .information_checks import attempt_identity
        attempts = [row for row in _rows(connection,prefix+"_check_attempts","1=1",()) if attempt_identity(row,prefix) in ids]
        tables[prefix+"_check_attempts"] = list({row["attempt_id"]:row for row in tables.get(prefix+"_check_attempts",[])+attempts}.values())
        run_ids = {row["run_id"] for row in tables[prefix+"_check_attempts"]}
        dependencies[prefix+"_check_runs"] = [row for row in _rows(connection,prefix+"_check_runs","1=1",())
            if row["run_id"] in run_ids or any(target.get("occurrence_id") in ids for target in json.loads(row["input_json"]).get("targets",[]))]
    # Stage supplement records are evidence, not the already public business state.
    tables["knowledge_versions"] = [row for row in tables["knowledge_versions"]
        if not row["source_ref"].startswith(("registry-supplement:", "pdf-classification:"))]
    document_ids = {row["source_document_sha256"] for table in ("pdf_intake_article_occurrences","pdf_intake_calendar_items") for row in tables.get(table,[])}
    if document_ids:
        dependencies["pdf_intake_documents"] = [row for identity in sorted(document_ids) for row in _rows(connection,"pdf_intake_documents","document_sha256=?",(identity,))]
        dependencies["pdf_intake_document_sources"] = [row for identity in sorted(document_ids) for row in _rows(connection,"pdf_intake_document_sources","document_sha256=?",(identity,))]
    report_ids = {row["report_id"] for row in tables.get("report_appearances",[])}
    if report_ids:
        dependencies["reports"] = [row for identity in sorted(report_ids) for row in _rows(connection,"reports","report_id=?",(identity,))]
    report_fallbacks = {}
    from dataclasses import asdict
    from .reports import parse_historical_report
    from climate_delivery.errors import ClimateDeliveryError
    source_dir = Path(source_dir) if source_dir is not None else Path(os.getenv("SOURCE_DIR") or Path(__file__).resolve().parents[1]/"sources")
    report_cache = {} if report_cache is None else report_cache
    for row in dependencies.get("reports",[]):
        key = (row["filename"],row["report_sha256"])
        if key not in report_cache:
            report_cache[key]=None
            if row["filename"] == "climate-monitor-"+row["report_date"]+".md":
                try:
                    parsed=parse_historical_report(source_dir/row["filename"],allow_offcycle=True)
                    if parsed.sha256 == row["report_sha256"]:
                        report_cache[key] = {**asdict(parsed),"path":row["filename"]}
                except (ClimateDeliveryError,OSError,UnicodeError,ValueError):
                    pass
        if report_cache[key] is not None:
            report_fallbacks[row["report_date"]]=report_cache[key]
    source_ids = [row["source_id"] for row in tables.get("articles", [])]
    sources = [_rows(connection, "sources", "source_id=?", (key,))[0] for key in source_ids]
    if annotations is None:
        from .annotations import load_article_annotations
        annotations = load_article_annotations(Path(os.getenv("ARTICLE_METADATA_DIR") or Path(__file__).resolve().parents[1] / "article_metadata"))
    from dataclasses import asdict
    urls = [row["canonical_url"] for row in tables.get("articles", [])]
    metadata = {url: asdict(annotations[url]) for url in urls if url in annotations}
    supplement = []
    if include_pending:
        for row in knowledge:
            prefixes = ("registry-supplement:", "pdf-classification:") if kind == "article" else ("pdf-classification:",) if kind == "pdf_article" else ("__none__",)
            if row["source_ref"].startswith(prefixes):
                evidence = json.loads(row["evidence_json"])
                if evidence.get("pending_enrichment"):
                    supplement.append(row)
    return {"schema_version": "registry-public-candidate.v1", "entity_kind": kind,
        "entity_id": entity_id, "tables": tables, "sources": sources,
        "fallback_annotations": metadata, "report_fallbacks": report_fallbacks, "supplements": supplement, "dependencies": dependencies,
        "information_targets":targets}


def stage_snapshot(connection, snapshot, *, basis="pending", created_at=None):
    previous = connection.execute("""SELECT c.snapshot_json FROM registry_publication p JOIN registry_candidates c
        ON c.candidate_sha256=p.latest_candidate_sha256 WHERE p.entity_kind=? AND p.entity_id=?""",
        (snapshot["entity_kind"], snapshot["entity_id"])).fetchone()
    if previous and "derived_display" not in snapshot:
        old = json.loads(previous[0])
        display = old.pop("derived_display", None)
        def business_rows(value):
            value=json.loads(json.dumps(value))
            value.pop("information_targets",None)
            for prefix in ("article","meeting"):
                value["tables"].pop(prefix+"_check_attempts",None)
                value["dependencies"].pop(prefix+"_check_runs",None)
            value["tables"]["knowledge_versions"]=[row for row in value["tables"].get("knowledge_versions",[]) if row["source_kind"]!="information_check"]
            return value
        if display and digest(business_rows(old)) == digest(business_rows(snapshot)):
            snapshot["derived_display"] = display
    sha = digest(snapshot)
    connection.execute("INSERT OR IGNORE INTO registry_candidates VALUES(?,?,?,?,?,?)",
        (sha, snapshot["entity_kind"], snapshot["entity_id"], json.dumps(snapshot, sort_keys=True, ensure_ascii=False),
         created_at or now_stamp(), basis))
    identity=(snapshot["entity_kind"],snapshot["entity_id"])
    current_row=connection.execute("SELECT is_visible FROM registry_publication WHERE entity_kind=? AND entity_id=?",identity).fetchone()
    current=current_row[0] if current_row else None
    aliases=[]
    if identity[0]=="article":
        linked=connection.execute("""SELECT p.entity_kind,p.entity_id,p.is_visible FROM registry_publication p
            JOIN pdf_intake_articles pdf ON pdf.article_id=p.entity_id JOIN articles a
            ON pdf.core_article_id=a.article_id OR pdf.canonical_url=a.canonical_url
            WHERE p.entity_kind='pdf_article' AND a.article_id=?""",(identity[1],)).fetchall()
        aliases=[row for row in linked if canonical_entity(connection,row[0],row[1])==identity]
    elif identity[0]=="meeting":
        linked=connection.execute("""SELECT DISTINCT p.entity_kind,p.entity_id,p.is_visible FROM registry_publication p
            JOIN pdf_intake_calendar_items c ON c.event_id=p.entity_id
            JOIN meeting_check_attempts check_row ON check_row.occurrence_id=c.occurrence_id
            WHERE p.entity_kind='pdf_meeting' AND json_extract(check_row.packet_json,'$.canonical_event_id')=?""",(identity[1],)).fetchall()
        aliases=[row for row in linked if canonical_entity(connection,row[0],row[1])==identity]
    visible=min(([current] if current is not None else [])+[flag for _,_,flag in aliases],default=1)
    connection.execute("""INSERT INTO registry_publication(entity_kind,entity_id,latest_candidate_sha256,is_visible)
        VALUES(?,?,?,?) ON CONFLICT(entity_kind,entity_id) DO UPDATE SET latest_candidate_sha256=excluded.latest_candidate_sha256,is_visible=excluded.is_visible""",
        (*identity,sha,visible))
    for kind,key,_ in aliases:
        connection.execute("UPDATE registry_publication SET is_visible=? WHERE entity_kind=? AND entity_id=?",(visible,kind,key))
    return sha


def stage_entities(connection, *, article_ids=None, annotations=None, source_dir=None):
    if not enabled(connection):
        return []
    staged = []
    report_cache = {}
    information_groups = _information_groups(connection)
    from .annotations import load_article_annotations
    if annotations is None:
        annotations = load_article_annotations(Path(os.getenv("ARTICLE_METADATA_DIR") or Path(__file__).resolve().parents[1] / "article_metadata"))
    for kind, table, column in (("article", "articles", "article_id"),
        ("pdf_article", "pdf_intake_articles", "article_id"), ("meeting", "climate_events", "event_id"),
        ("pdf_meeting", "pdf_intake_calendar_items", "event_id")):
        for (entity_id,) in connection.execute(f"SELECT DISTINCT {column} FROM {table} ORDER BY {column}").fetchall():
            if canonical_entity(connection, kind, entity_id) != (kind, entity_id):
                continue
            if article_ids is not None and kind == "article" and entity_id not in article_ids:
                continue
            staged.append(stage_snapshot(connection, snapshot_entity(connection, kind, entity_id, annotations=annotations,source_dir=source_dir,report_cache=report_cache,information_groups=information_groups)))
    return staged


def information_ready(connection, snapshot, *, information_groups=None):
    """T10 consumes real terminal T1 results for this exact current source/version."""
    from .information_checks import _sha, attempt_identity, CHECK_VERSION
    from .acquisition_review import timestamp
    targets = snapshot.get("information_targets",[])
    current = (information_groups if information_groups is not None else _information_groups(connection)).get(
        (snapshot["entity_kind"],snapshot["entity_id"]),[])
    if not targets or targets != current:
        return False
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve()
    for target in targets:
        prefix = "article" if target["check_kind"]=="articles" else "meeting"
        frozen_target = {key:value for key,value in target.items() if key!="check_kind"}
        relevant = []
        run_order=dict(connection.execute("SELECT run_id,rowid FROM "+prefix+"_check_runs"))
        for run in _rows(connection,prefix+"_check_runs","1=1",()):
            try:
                frozen = json.loads(run["input_json"])
                if frozen_target not in frozen.get("targets",[]):
                    continue
                if hashlib.sha256(run["input_json"].encode()).hexdigest() != run["input_sha256"]:
                    return False
                if frozen.get("schema_version")!=CHECK_VERSION or Path(frozen["registry_database"]).resolve() != database or frozen.get("kind")!=target["check_kind"]:
                    return False
                relevant.append((timestamp(run["created_at"]),run_order[run["run_id"]],run))
            except (ValueError,KeyError,TypeError):
                return False
        if not relevant:
            return False
        run = max(relevant,key=lambda value:value[:2])[2]
        if (run not in snapshot.get("dependencies",{}).get(prefix+"_check_runs",[])
                or run["status"] not in {"complete","partial"} or not run["completed_at"]
                or run["item_count"]<=0 or run["completed_count"]!=run["item_count"]):
            return False
        if len(json.loads(run["input_json"])["targets"])!=run["item_count"]:
            return False
        cursor = connection.execute("SELECT * FROM "+prefix+"_check_runs WHERE run_id=?",(run["run_id"],))
        actual = cursor.fetchone()
        if actual is None or dict(zip((item[0] for item in cursor.description),actual)) != run:
            return False
        attempts = [row for row in snapshot["tables"].get(prefix+"_check_attempts",[])
            if row["run_id"]==run["run_id"] and attempt_identity(row,prefix)==target["occurrence_id"] and row["source_url"]==target["source_url"]]
        if len(attempts)!=1:
            return False
        attempt = attempts[0]
        try:
            packet = json.loads(attempt["packet_json"])
            timestamp(attempt["checked_at"])
            if (_sha(packet)!=attempt["packet_sha256"] or packet.get("checked_at")!=attempt["checked_at"]
                    or attempt["source_revision_sha256"]!=target["source_revision_sha256"]
                    or any(packet.get(key)!=value for key,value in frozen_target.items())
                    or packet.get("access_status")!=attempt["access_status"]
                    or packet.get("verification_status")!=attempt["verification_status"]):
                return False
        except (ValueError,KeyError,TypeError):
            return False
    return True


def require_review_information(connection, packet):
    """Check before native ownership or history mutations, including old packets."""
    groups = _information_groups(connection)
    pending = False
    for item in packet.get("candidates",[]):
        if packet.get("schema_version")!="registry-final-review.v1" and not item.get("registry_candidate_sha256"):
            raise ValueError("historical source-only candidate requires a new source-bound review packet")
        row = connection.execute("""SELECT c.snapshot_json,p.latest_candidate_sha256,p.published_candidate_sha256
            FROM registry_candidates c JOIN registry_publication p ON p.entity_kind=c.entity_kind AND p.entity_id=c.entity_id
            WHERE c.candidate_sha256=?""",((item.get("registry_candidate_sha256") or item["candidate_sha256"]),)).fetchone()
        if row is None or row[1]!=(item.get("registry_candidate_sha256") or item["candidate_sha256"]) or not information_ready(connection,json.loads(row[0]),information_groups=groups):
            raise ValueError("candidate awaits completed source-bound T1 information improvement")
        pending |= row[2]!=row[1]
    if not pending and packet.get("schema_version")=="registry-final-review.v1":
        raise ValueError("no current T1-completed candidates await final review")


def _approve(connection, sha, receipt, *, status="pass", information_groups=None):
    row = connection.execute("SELECT entity_kind,entity_id,snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()
    if row is None or digest(json.loads(row[2])) != sha:
        raise ValueError("candidate snapshot identity differs")
    latest = connection.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", row[:2]).fetchone()
    if latest is None or (status in {"pass", "accepted_legacy"} and latest[0] != sha):
        raise ValueError("changed candidate requires another final review")
    if status == "pass" and not information_ready(connection,json.loads(row[2]),information_groups=information_groups):
        raise ValueError("candidate awaits completed source-bound T1 information improvement")
    review_id = digest([sha, status, receipt])
    connection.execute("INSERT OR IGNORE INTO registry_reviews VALUES(?,?,?,?,?)",
        (review_id, sha, status, now_stamp(), json.dumps(receipt, sort_keys=True, ensure_ascii=False)))
    if status in {"pass", "accepted_legacy"}:
        connection.execute("UPDATE registry_publication SET published_candidate_sha256=? WHERE entity_kind=? AND entity_id=?", (sha, *row[:2]))
    return review_id


def set_visibility(database, kind, entity_id, is_visible):
    if type(is_visible) is not bool:
        raise ValueError("is_visible must be boolean")
    from .information_checks import _writer
    with _writer(resolve_database(database)) as connection:
        kind, entity_id = canonical_entity(connection, kind, entity_id)
        identities = [(row[0], row[1]) for row in connection.execute("SELECT entity_kind,entity_id FROM registry_publication")
            if canonical_entity(connection, row[0], row[1]) == (kind, entity_id)]
        cursor = connection.execute("UPDATE registry_publication SET is_visible=? WHERE entity_kind=? AND entity_id=?", (int(is_visible), kind, entity_id))
        for alias_kind, alias_id in identities:
            connection.execute("UPDATE registry_publication SET is_visible=? WHERE entity_kind=? AND entity_id=?", (int(is_visible), alias_kind, alias_id))
        if cursor.rowcount != 1:
            raise ValueError("Registry identity not found")


def public_connection(source):
    """Restore only approved immutable rows, then expose a query-only connection."""
    if not enabled(source):
        return source
    from .schema import apply_migrations
    projection = sqlite3.connect(":memory:")
    projection.row_factory = sqlite3.Row
    try:
        apply_migrations(projection)
        for (name,) in projection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            projection.execute(f'DROP TRIGGER "{name}"')
        projection.execute("PRAGMA foreign_keys=OFF")
        for (table,) in source.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if table.startswith("sqlite_") or table in PROJECTED_TABLES or table in {"schema_migrations", "registry_candidates", "registry_reviews", "registry_publication"}:
                continue
            cursor = source.execute(f'SELECT * FROM "{table}"')
            values = cursor.fetchall()
            if values:
                projection.executemany(f'INSERT INTO "{table}" VALUES({",".join("?" for _ in cursor.description)})', values)
        selected = source.execute("""SELECT c.candidate_sha256,c.snapshot_json FROM registry_publication p
            JOIN registry_candidates c ON c.candidate_sha256=p.published_candidate_sha256
            WHERE p.is_visible=1 AND EXISTS(SELECT 1 FROM registry_reviews r
                WHERE r.candidate_sha256=c.candidate_sha256 AND r.status IN ('pass','accepted_legacy'))""").fetchall()
        projection.execute("CREATE TEMP TABLE public_snapshot_metadata(entity_kind TEXT,entity_id TEXT,snapshot_json TEXT)")
        projection.execute("CREATE TEMP TABLE approved_public_reviews(entity_kind TEXT,entity_id TEXT,approved_at TEXT)")
        projection.executemany("INSERT INTO approved_public_reviews VALUES(?,?,?)", source.execute("SELECT p.entity_kind,p.entity_id,min(r.reviewed_at) FROM registry_publication p JOIN registry_reviews r ON r.candidate_sha256=p.published_candidate_sha256 WHERE p.is_visible=1 AND r.status='pass' GROUP BY p.entity_kind,p.entity_id").fetchall())
        snapshots = []
        published = {}
        for sha, encoded in selected:
            snapshot = json.loads(encoded)
            if digest(snapshot) != sha:
                raise ValueError("published Registry snapshot hash differs")
            published[(snapshot["entity_kind"], snapshot["entity_id"])] = snapshot
        for sha, encoded in selected:
            snapshot = json.loads(encoded)
            canonical = canonical_entity(source, snapshot["entity_kind"], snapshot["entity_id"])
            if canonical != (snapshot["entity_kind"], snapshot["entity_id"]):
                state = source.execute("SELECT is_visible,published_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", canonical).fetchone()
                # A pending raw alias cannot replace an older approved observation.
                # Suppress it only when the approved canonical snapshot absorbed it.
                canonical_snapshot = published.get(canonical)
                table = "pdf_intake_article_occurrences" if snapshot["entity_kind"] == "pdf_article" else "pdf_intake_calendar_items"
                observations = {row["occurrence_id"] for row in snapshot["tables"].get(table, [])}
                absorbed = canonical_snapshot is not None and bool(observations) and observations <= {
                    row["occurrence_id"] for row in canonical_snapshot["tables"].get(table, [])}
                if state and (not state[0] or absorbed):
                    continue
            snapshots.append(snapshot)
            projection.execute("INSERT INTO public_snapshot_metadata VALUES(?,?,?)", (snapshot["entity_kind"], snapshot["entity_id"], encoded))
        for snapshot in snapshots:
            for row in snapshot["sources"]:
                projection.execute("UPDATE sources SET hostname=?,display_name=?,first_seen=?,last_seen=? WHERE source_id=?",
                    (row["hostname"], row["display_name"], row["first_seen"], row["last_seen"], row["source_id"]))
            for table, rows in {**snapshot.get("dependencies", {}), **snapshot["tables"]}.items():
                for row in rows:
                    row = dict(row)
                    for column, value in row.items():
                        if isinstance(value,dict) and set(value)=={"blob_sha256","blob_size"}:
                            import hashlib
                            identity=row["document_sha256"]
                            blob=source.execute(f'SELECT "{column}" FROM "{table}" WHERE document_sha256=?',(identity,)).fetchone()[0]
                            if blob is None or len(blob)!=value["blob_size"] or hashlib.sha256(blob).hexdigest()!=value["blob_sha256"]:
                                raise ValueError("approved PDF bytes identity differs")
                            row[column]=blob
                    columns = list(row)
                    projection.execute(f'INSERT OR REPLACE INTO "{table}" ({",".join(columns)}) VALUES({",".join("?" for _ in columns)})', tuple(row.values()))
        # An invisible core identity also excludes linked PDF observations.
        hidden = [row[0] for row in source.execute("SELECT entity_id FROM registry_publication WHERE entity_kind='article' AND is_visible=0")]
        for identity in hidden:
            projection.execute("DELETE FROM pdf_intake_article_occurrences WHERE article_id IN (SELECT article_id FROM pdf_intake_articles WHERE core_article_id=?)", (identity,))
            projection.execute("DELETE FROM pdf_intake_articles WHERE core_article_id=?", (identity,))
        projection.commit()
        projection.execute("PRAGMA query_only=ON")
        projection.execute("PRAGMA trusted_schema=OFF")
        return projection
    except BaseException:
        projection.close()
        raise


def prepare_review(database, root):
    """Freeze pending candidates for the existing T1/T10 native review context."""
    from .information_checks import _writer
    from climate_delivery.io import atomic_write_json
    from .acquisition_review import freeze_readable
    database, root = resolve_database(database), Path(root)
    claim_path = root / "claim.json"
    packet_path = root / "packet.json"
    if claim_path.exists() and packet_path.exists():
        from .acquisition_review import owner_finished
        claim = json.loads(claim_path.read_text())
        if not claim.get("released_at") and not owner_finished(claim):
            packet = json.loads(packet_path.read_text())
            resolve_database(database,frozen=packet["registry_database"])
            return packet  # The native owner finishes its exact immutable packet.
    with _writer(database) as connection:
        stage_entities(connection)
        groups = _information_groups(connection)
        candidates = []
        for row in connection.execute("""SELECT c.* FROM registry_publication p JOIN registry_candidates c
            ON c.candidate_sha256=p.latest_candidate_sha256
            WHERE p.published_candidate_sha256 IS NOT p.latest_candidate_sha256"""):
            snapshot = json.loads(row[3])
            if not information_ready(connection,snapshot,information_groups=groups):
                continue
            path = root / "evidence" / (row[0] + ".json")
            atomic_write_json(path, snapshot)
            view = freeze_readable(path.with_suffix(".readable.json"), path.read_text(encoding="utf-8"))
            candidates.append({"candidate_sha256": row[0], "entity_kind": row[1], "entity_id": row[2], "snapshot_path": str(path.resolve()), "snapshot_view": view})
    packet = {"schema_version": "registry-final-review.v1", "registry_database": str(database), "run_id": "registry-review", "candidates": candidates}
    atomic_write_json(root / "packets" / (digest(packet)+".json"),packet)
    atomic_write_json(root / "packet.json", packet)
    from .acquisition_review import refresh_acquisition_reviews
    refresh_acquisition_reviews(database,root.parent)
    return packet


def review_candidates(root, token, conclusions):
    from .acquisition_review import validate_claim, read_verified_text
    from .information_checks import _writer
    from climate_delivery.io import atomic_write_json
    root = Path(root)
    packet = json.loads((root / "packet.json").read_text(encoding="utf-8"))
    claim, events = validate_claim(root, packet, token)
    outcomes = []
    with _writer(resolve_database(frozen=packet["registry_database"])) as connection:
        stage_entities(connection)
        groups = _information_groups(connection)
        for item in packet["candidates"]:
            sha = item["candidate_sha256"]
            conclusion = conclusions.get(sha)
            if not conclusion:
                continue
            if conclusion.get("candidate_sha256") != sha or conclusion.get("status") not in {"pass", "needs_correction", "rejected"} or not conclusion.get("reason"):
                raise ValueError("final review must identify the exact candidate and reason")
            text = Path(item["snapshot_path"]).read_text(encoding="utf-8")
            if digest(json.loads(text)) != sha:
                raise ValueError("reviewed snapshot changed")
            inspection = read_verified_text(events, item["snapshot_path"], text, item.get("snapshot_view"))
            receipt = {"claim": claim, "conclusion": conclusion, "inspection": inspection, "events_sha256": digest(events)}
            try:
                outcomes.append(_approve(connection, sha, receipt, status=conclusion["status"],information_groups=groups))
            except ValueError as exc:
                outcomes.append(_approve(connection, sha, {**receipt, "final_check_error": str(exc)}, status="needs_correction"))
    atomic_write_json(root / "receipts" / (digest(outcomes) + ".json"), {"reviews": outcomes, "claim": claim})
    atomic_write_json(root / "claim.json", {**claim, "released_at": now_stamp()})
    return {"reviewed": len(outcomes), "review_ids": outcomes}


def review_activation_status(root, *, database=None):
    """Report this packet's actual review and current pointers without queuing work."""
    root = Path(root)
    packet = json.loads((root / "packet.json").read_text(encoding="utf-8"))
    selected = resolve_database(database, frozen=packet["registry_database"])
    if os.getenv("CLIMATE_REGISTRY_DB", "").strip():
        resolve_database(frozen=packet["registry_database"])
    candidates = {item["candidate_sha256"] for item in packet["candidates"]}
    result = {"status": "pending_review" if candidates else "no_approved_candidates",
        "candidate_count": len(candidates), "reviewed_count": 0, "approved_count": 0,
        "published_count": 0, "needs_correction_count": 0, "rejected_count": 0,
        "pending_count": len(candidates), "queued_count": 0}
    claim_path = root / "claim.json"
    if not claim_path.is_file():
        return result
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    if claim.get("packet_sha256") != digest(packet):
        return result
    review_ids = set()
    for path in (root / "receipts").glob("*.json"):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if (receipt.get("claim") or {}).get("token") == claim["token"]:
            review_ids.update(receipt.get("reviews", []))
    reviewed = {}
    with sqlite3.connect(f"{selected.as_uri()}?mode=ro", uri=True) as connection:
        for review_id in review_ids:
            row = connection.execute("""SELECT r.candidate_sha256,r.status,r.receipt_json,
                p.latest_candidate_sha256,p.published_candidate_sha256,p.is_visible
                FROM registry_reviews r JOIN registry_candidates c ON c.candidate_sha256=r.candidate_sha256
                JOIN registry_publication p ON p.entity_kind=c.entity_kind AND p.entity_id=c.entity_id
                WHERE r.review_id=?""", (review_id,)).fetchone()
            if row is None or row[0] not in candidates:
                continue
            evidence = json.loads(row[2]).get("claim") or {}
            if evidence.get("token") == claim["token"] and evidence.get("packet_sha256") == digest(packet):
                reviewed[row[0]] = row
    result["reviewed_count"] = len(reviewed)
    result["pending_count"] = len(candidates - reviewed.keys())
    for sha, row in reviewed.items():
        if row[1] == "pass" and row[3] == sha and row[4] == sha:
            result["approved_count"] += 1
            result["published_count"] += int(bool(row[5]))
        elif row[1] in {"needs_correction", "rejected"}:
            result[row[1] + "_count"] += 1
        else:
            result["pending_count"] += 1
    approved, published = result["approved_count"], result["published_count"]
    if approved:
        result["status"] = ("published" if published == approved else "approved") if approved == len(candidates) else (
            "partially_published" if published else "partially_approved")
    elif result["needs_correction_count"]:
        result["status"] = "needs_correction"
    elif result["reviewed_count"]:
        result["status"] = "no_approved_candidates"
    return result


def migrate_publication(database, backup_dir, *, apply=False):
    """One-time schema migration: accept the actual legacy view, then stage updates."""
    from .persistent import _exclusive_database_lock, _backup_name, _sqlite_sidecars
    from .schema import apply_migrations
    from .contract import validate_registry_contract
    database = resolve_database(database)
    with _exclusive_database_lock(database):
        with sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            version = validate_registry_contract(connection)
            if not apply:
                has_pdf = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_intake_articles'").fetchone() is not None
                return {"status": "dry_run", "schema_version": version, "target_version": 22,
                    "core_articles": connection.execute("SELECT count(*) FROM articles").fetchone()[0],
                    "pdf_articles": connection.execute("SELECT count(*) FROM pdf_intake_articles").fetchone()[0] if has_pdf else 0}
            if version < 22:
                backup_dir = Path(backup_dir)
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup = backup_dir / _backup_name(database)
                if _sqlite_sidecars(database):
                    raise ValueError("Registry migration requires checkpointed SQLite sidecars")
                import shutil
                with database.open("rb") as source, backup.open("xb") as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                apply_migrations(connection)
            else:
                backup = None
            if version >= 21:
                return {"status":"migrated","schema_version":22,"accepted_legacy":0,"manual_materialized":0,
                    "manual_published":0,"staged":0,
                    "core_supplements":connection.execute("SELECT count(*) FROM knowledge_versions WHERE source_ref LIKE 'registry-supplement:%'").fetchone()[0],
                    "pdf_classifications":connection.execute("SELECT count(*) FROM knowledge_versions WHERE source_ref LIKE 'pdf-classification:%'").fetchone()[0],
                    "backup":str(backup) if backup else None}
            accepted = 0
            with connection:
                information_groups = _information_groups(connection)
                for kind, table, column in (("article", "articles", "article_id"), ("pdf_article", "pdf_intake_articles", "article_id"),
                    ("meeting", "climate_events", "event_id"), ("pdf_meeting", "pdf_intake_calendar_items", "event_id")):
                    for (identity,) in connection.execute(f"SELECT DISTINCT {column} FROM {table}").fetchall():
                        if version >= 21:
                            continue
                        if canonical_entity(connection, kind, identity) != (kind, identity):
                            continue
                        if connection.execute("SELECT 1 FROM registry_publication WHERE entity_kind=? AND entity_id=?", (kind, identity)).fetchone():
                            continue
                        sha = stage_snapshot(connection, snapshot_entity(connection, kind, identity, include_pending=False,information_groups=information_groups), basis="accepted_legacy")
                        _approve(connection, sha, {"basis": "pre-migration actual public Registry rows", "previous_schema": version}, status="accepted_legacy")
                        accepted += 1
            with connection:
                materialized = materialize_manual(connection)
                staged = stage_entities(connection)
                supplemented = 0
                for row in connection.execute("""SELECT c.candidate_sha256,c.snapshot_json FROM registry_publication p
                    JOIN registry_candidates c ON c.candidate_sha256=p.latest_candidate_sha256
                    WHERE p.published_candidate_sha256 IS NOT p.latest_candidate_sha256""").fetchall():
                    snapshot = json.loads(row[1])
                    baseline_row = connection.execute("SELECT c.snapshot_json FROM registry_publication p JOIN registry_candidates c ON c.candidate_sha256=p.published_candidate_sha256 WHERE p.entity_kind=? AND p.entity_id=?", (snapshot["entity_kind"],snapshot["entity_id"])).fetchone()
                    baseline = json.loads(baseline_row[0]) if baseline_row else None
                    comparable = json.loads(json.dumps(snapshot))
                    comparable["supplements"] = []
                    comparable.pop("information_targets",None)
                    if baseline:
                        baseline.pop("information_targets",None)
                    baseline_ids = {item["enrichment_id"] for item in (baseline or {}).get("tables",{}).get("article_enrichments",[])}
                    flagged_ids = {"manual-"+digest([item["knowledge_id"],json.loads(item["evidence_json"])])[:32] for item in snapshot.get("supplements",[]) if json.loads(item["evidence_json"]).get("materialize_manual_enrichment")} - baseline_ids
                    if "article_enrichments" in comparable["tables"]:
                        comparable["tables"]["article_enrichments"] = [item for item in comparable["tables"]["article_enrichments"] if item["enrichment_id"] not in flagged_ids]
                    if snapshot.get("supplements") and baseline and not baseline.get("supplements") and digest(baseline)==digest(comparable):
                        _approve(connection, row[0], {"basis": "owner-approved historical manual supplement",
                            "source_rows": [json.loads(item["evidence_json"])["source_row_sha256"] for item in snapshot["supplements"]]}, status="accepted_legacy")
                        supplemented += 1
            return {"status": "migrated", "schema_version": 22, "accepted_legacy": accepted,
                "manual_materialized": materialized, "manual_published": supplemented,
                "staged": len(staged), "core_supplements": connection.execute("SELECT count(*) FROM knowledge_versions WHERE source_ref LIKE 'registry-supplement:%'").fetchone()[0],
                "pdf_classifications": connection.execute("SELECT count(*) FROM knowledge_versions WHERE source_ref LIKE 'pdf-classification:%'").fetchone()[0],
                "backup": str(backup) if backup else None}


def materialize_manual(connection):
    """Only the seven explicitly approved core supplements become manual rows."""
    added = 0
    for row in _rows(connection, "knowledge_versions", "source_ref LIKE 'registry-supplement:%'", ()):
        evidence = json.loads(row["evidence_json"])
        if not evidence.get("materialize_manual_enrichment"):
            continue
        pending = evidence["pending_enrichment"]
        content_id, version_id = evidence.get("content_version_id"), evidence.get("article_version_id")
        if not content_id and not version_id:
            raise ValueError("manual supplement lacks an exact article version")
        if content_id:
            content = connection.execute("SELECT content_sha256 FROM article_content_versions WHERE content_version_id=? AND article_id=?", (content_id, row["entity_id"])).fetchone()
            if content is None or content[0] != evidence.get("content_sha256"):
                raise ValueError("manual supplement body identity differs")
        key = "manual-" + digest([row["knowledge_id"], evidence])[:32]
        cursor = connection.execute("""INSERT OR IGNORE INTO article_enrichments(
            enrichment_id,article_id,content_version_id,article_version_id,status,summary,categories_json,keywords_json,
            language,generator_kind,generator_name,generator_version,generated_at,error_code,error_message)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)""", (key, row["entity_id"], content_id,
                None if content_id else version_id, "complete", pending["summary"], json.dumps(pending["categories"]),
                json.dumps(pending["keywords"]), pending["language"], "manual", "registry-supplement",
                evidence.get("source_row_sha256") or row["knowledge_id"], row["recorded_at"]))
        added += cursor.rowcount
    return added


def snapshot_metadata(connection, kind, entity_id):
    if not enabled(connection) or not connection.execute("SELECT 1 FROM sqlite_temp_master WHERE name='public_snapshot_metadata'").fetchone():
        return None
    kind, entity_id = canonical_entity(connection, kind, entity_id)
    row = connection.execute("SELECT snapshot_json FROM public_snapshot_metadata WHERE entity_kind=? AND entity_id=?", (kind, entity_id)).fetchone()
    return json.loads(row[0]) if row else None


def _core_supplement_fields(snapshot, item, evidence):
    """The one-time supplement belongs to its original version/enrichment."""
    article=next(row for row in snapshot["tables"]["articles"] if row["article_id"]==snapshot["entity_id"])
    if "article_version_id" in evidence and evidence["article_version_id"]!=article["current_version_id"]:
        return set()
    if "content_version_id" in evidence and evidence["content_version_id"]!=article.get("current_content_version_id"):
        return {"title"}
    if evidence.get("content_sha256"):
        content=next((row for row in snapshot["tables"]["article_content_versions"] if row["content_version_id"]==article.get("current_content_version_id")),None)
        if not content or content["content_sha256"]!=evidence["content_sha256"]:
            return {"title"}
    candidates=[row for row in snapshot["tables"].get("article_enrichments",[]) if row["status"]=="complete" and (
        (article.get("current_content_version_id") is not None and row.get("content_version_id")==article["current_content_version_id"]) or
        (row.get("content_version_id") is None and row.get("article_id")==article["article_id"] and row.get("article_version_id")==article["current_version_id"]))]
    selected=max(candidates,key=lambda row:(row.get("content_version_id") is not None,row["generated_at"],row["enrichment_id"]),default=None)
    if selected is None:
        return {"title","summary","categories","keywords"}
    original={json.loads(row["evidence_json"]).get("enrichment_id") for row in snapshot["tables"].get("knowledge_versions",[]) if
        row["entity_id"]==article["article_id"] and row["source_ref"]=="legacy-core:"+article["article_id"]}
    if evidence.get("materialize_manual_enrichment"):
        original.add("manual-"+digest([item["knowledge_id"],evidence])[:32])
    return {"title","summary","categories","keywords"} if selected["enrichment_id"] in original else {"title"}


def approved_display(snapshot):
    """A reused report/PDF summary keeps its observed provenance after classification."""
    display = {}
    for item in snapshot.get("supplements", []):
        if snapshot["entity_kind"] == "article" and not item["source_ref"].startswith("registry-supplement:"):
            continue
        evidence = json.loads(item["evidence_json"])
        pending = evidence["pending_enrichment"]
        fields=_core_supplement_fields(snapshot,item,evidence) if snapshot["entity_kind"]=="article" else {"title","summary","categories","keywords"}
        display.update({key: value for key, value in pending.items() if key in fields and value is not None})
        if "summary" not in fields:
            continue
        source = evidence.get("field_sources", {}).get("summary")
        display["summary_provenance"] = ("content_enrichment" if source == "current_content_enrichment"
            else "source_report" if source in {"observed_summary", "report_summary"}
            else evidence.get("summary_basis") or "manual_enrichment")
        display["metadata_provenance"] = {"categories": "manual_supplement", "keywords": "manual_supplement"}
        display["supplement_provenance"] = evidence
        display["supplement_generator"] = {"kind": "manual", "name": "registry-supplement",
            "version": evidence["source_row_sha256"]}
    display.update(snapshot.get("derived_display", {}))
    return display


def public_revision(database, *, connection=None):
    if connection is not None:
        if not enabled(connection):
            return None
        return digest([tuple(row) for row in connection.execute("SELECT entity_kind,entity_id,is_visible,published_candidate_sha256 FROM registry_publication ORDER BY entity_kind,entity_id")])
    from .read_api import RegistryReader
    with RegistryReader(database, repository_root=Path(__file__).resolve().parents[1], public=False).connect() as connection:
        return public_revision(database, connection=connection)


def public_wiki_snapshot(database, *, wiki_dir=None):
    """Bind public pages to the revision from the same frozen Registry read."""
    from .read_api import RegistryReader
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])
    with reader.public_snapshot():
        with reader.connect() as connection:
            revision = public_revision(database, connection=getattr(reader, "_snapshot_source_connection", connection))
        if revision is None and os.getenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT") != "1":
            return None, None
        return public_wiki_pages(database, wiki_dir=wiki_dir, reader=reader), revision


def public_wiki_pages(database, *, wiki_dir=None, reader=None, source_dir=None):
    from tempfile import TemporaryDirectory
    from .wiki import sync_registry_wiki
    static_dir = Path(wiki_dir) if wiki_dir is not None else Path(__file__).resolve().parents[1] / "wiki"
    static_path = static_dir / "public-registry.json"
    if os.getenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT") == "1":
        if static_path.is_file():
            artifact = json.loads(static_path.read_text(encoding="utf-8"))
            sha = artifact.pop("artifact_sha256")
            if digest(artifact) != sha:
                raise ValueError("Git public Wiki snapshot identity differs")
            return artifact["wiki_pages"]
        return {path.name: path.read_text(encoding="utf-8") for path in sorted(static_dir.glob("*.md"))}
    if reader is None and public_revision(database) is None:
        return None
    with TemporaryDirectory(prefix="climate-public-wiki-") as temporary:
        directory = Path(temporary)
        if reader is None:
            sync_registry_wiki(Path(database), directory)
        else:
            from .wiki import _registry_pages_read
            _registry_pages_read(Path(database),directory,reader=reader)
        pages = {path.name: path.read_text(encoding="utf-8") for path in directory.glob("*.md")}
        if source_dir is not None:
            selected_reader = reader
            if selected_reader is None:
                from .read_api import RegistryReader
                selected_reader=RegistryReader(database,repository_root=Path(__file__).resolve().parents[1])
            with selected_reader.public_snapshot():
                with selected_reader.connect() as connection:
                    identities=[row[0] for row in connection.execute("SELECT article_id FROM articles WHERE publication_eligible=1 AND document_kind='article'")]
                details={item["canonical_url"]:item for item in (selected_reader.article(identity) for identity in identities)}
                from .reports import parse_report_directory
                from climate_monitor.dedupe import canonical_url
                for report in parse_report_directory(Path(source_dir),allow_offcycle=True):
                    blocks=[]
                    for article in report.articles:
                        detail=details.get(canonical_url(article.url))
                        if detail:
                            blocks += ["## "+str(detail.get("title") or detail["canonical_url"]),
                                detail.get("summary") or detail.get("report_summary") or "",
                                "Approved Registry version: "+detail["published_candidate_sha256"],detail["canonical_url"],""]
                    if blocks:
                        pages[report.path.name]=("# Climate Monitor "+report.report_date+"\n\nSource report: "+report.path.name+"; SHA-256: "+report.sha256+"\n\n"+"\n".join(blocks)).rstrip("\n")+"\n"
        pages["index.md"] = "# Climate Registry\n\n" + "\n".join(f"- [{name.removesuffix('.md')}]({name})" for name in sorted(pages)) + "\n"
        return pages


def public_meeting_coverage(coverage):
    """Keep the existing public meeting status/counts, never raw run evidence."""
    coverage = coverage if isinstance(coverage, dict) else {}
    result = {"status": coverage.get("status") if coverage.get("status") in {
        "processed", "partial", "failed", "running", "disabled", "enabled_unprocessed", "unavailable"
    } else "unavailable"}
    if coverage.get("records_scope") == "approved_versions":
        result["records_scope"] = "approved_versions"
    if type(coverage.get("returned_record_count")) is int and coverage["returned_record_count"] >= 0:
        result["returned_record_count"] = coverage["returned_record_count"]
    return result


def export_public_snapshot(database, output, *, reader=None, source_dir=None):
    """Export approved public data, never a business database or pending rows."""
    from .read_api import RegistryReader
    from climate_delivery.io import atomic_write_json
    database = resolve_database(database)
    reader = reader or RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])
    with reader.public_snapshot():
        with reader.connect() as connection:
            ids = [row[0] for row in connection.execute("SELECT article_id FROM articles")]
        pdf_reports = reader.pdf_reports_all()
        pdf_details = {}
        from .read_api import RegistryNotFoundError
        for report in pdf_reports:
            try:
                pdf_details[report["document_sha256"]] = _pdf_report_dto(reader.pdf_report(report["document_sha256"]))
            except RegistryNotFoundError:
                # Preserve the full-source archive gate when only some observations are public.
                pass
        value = {"schema_version": "climate-public-snapshot.v1",
            "articles": [_public_dto(reader.article(identity)) for identity in ids],
            "pdf_articles": [_pdf_dto(item) for item in reader.pdf_articles_all(include_linked=True)],
            "meetings": [_calendar_dto(item) for item in reader.pdf_calendar_items_all()], "meeting_records": [_calendar_dto(item) for item in reader.meetings_all(base_date="1900-01-01")],
            "meeting_coverage": public_meeting_coverage(reader.meetings(base_date="1900-01-01", page_size=1)["coverage"]),
            "pdf_reports": pdf_reports, "pdf_report_details": pdf_details,
            "wiki_pages": public_wiki_pages(database,reader=reader,source_dir=source_dir)}
    value["artifact_sha256"] = digest(value)
    atomic_write_json(Path(output), value)
    return {"status": "exported", "articles": len(value["articles"]),
        "pdf_articles": len(value["pdf_articles"]), "artifact_sha256": value["artifact_sha256"]}


def install_git_snapshot(database, wiki_dir):
    """The existing Render temporary DB can serve committed approved Git data."""
    from .information_checks import _writer
    wiki_dir = Path(wiki_dir)
    path = wiki_dir / "public-registry.json"
    artifact = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    if artifact:
        sha = artifact.pop("artifact_sha256")
        if artifact.get("schema_version") != "climate-public-snapshot.v1" or digest(artifact) != sha:
            raise ValueError("Git public snapshot identity differs")
        artifact["artifact_sha256"] = sha
    with _writer(Path(database)) as connection:
        if artifact:
            payloads = {item["article_id"]: item for item in artifact["articles"]}
            # The Git snapshot defines the current public allowlist; source files
            # remain an immutable archive and cannot republish excluded articles.
            connection.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_kind='article'")
            for identity, item in payloads.items():
                if connection.execute("SELECT 1 FROM articles WHERE article_id=?", (identity,)).fetchone():
                    continue
                source = connection.execute("SELECT source_id FROM sources WHERE hostname=?",(item["source"],)).fetchone()
                source_id = source[0] if source else "git-source-" + digest(item["source"])[:24]
                if source is None:
                    connection.execute("INSERT INTO sources VALUES(?,?,?,?,?)", (source_id, item["source"], item["publisher"], item["first_seen"], item["last_seen"]))
                connection.execute("""INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen,document_kind,publication_eligible,display_policy)
                    VALUES(?,?,?,?,?,?,?,?)""", (identity,item["canonical_url"],source_id,item["first_seen"],item["last_seen"],item["document_kind"],int(item["publication_eligible"]),item["display_policy"]))
        else:
            payloads = {}
        for (identity,) in connection.execute("SELECT article_id FROM articles").fetchall():
            snapshot = snapshot_entity(connection, "article", identity, include_pending=False)
            payload = payloads.get(identity)
            if artifact and payload is None:
                continue
            article_page = wiki_dir / ("article-" + identity + ".md")
            if payload:
                snapshot["static_payload"] = payload
            elif article_page.is_file():
                snapshot["git_wiki"] = {"markdown": article_page.read_text(encoding="utf-8"),
                    "sha256": digest(article_page.read_text(encoding="utf-8")), "basis": "approved committed Wiki"}
            sha = stage_snapshot(connection, snapshot, basis="accepted_legacy")
            _approve(connection, sha, {"basis": "approved committed Git snapshot"}, status="accepted_legacy")
            connection.execute("UPDATE registry_publication SET is_visible=1 WHERE entity_kind='article' AND entity_id=?", (identity,))
    return artifact


def _public_dto(payload):
    excluded = {"latest_fetch", "supplement_provenance"}
    value = {key: item for key, item in payload.items() if key not in excluded}
    if "acquisition_observations" in payload:
        value["acquisition_observations"] = [{key: item for key, item in row.items() if key in {
            "acquisition_item_id", "batch_id", "raw_url", "source_name", "title", "summary", "discovered_at",
            "discovery_kind", "discovery_ref", "publication_date", "publication_date_evidence", "date_status", "collected_at", "content_version_id", "origins"}}
            for row in payload.get("acquisition_observations", []) if row.get("processing_status", "complete") == "complete" and row.get("selection_status", "selected") == "selected"]
        for row in value["acquisition_observations"]:
            if "publication_date_evidence" in row:
                row["publication_date_evidence"] = _date_evidence_dto(row["publication_date_evidence"])
    if "date_observations" in payload:
        value["date_observations"] = [{key:item for key,item in row.items() if key in {"observation_id","kind","observed_at","recorded_at"}} | {
            "evidence":_date_evidence_dto(row.get("evidence",{}))}
            for row in payload.get("date_observations", [])]
    if "pdf_occurrences" in value:
        value["pdf_occurrences"]=[_occurrence_dto(item) for item in value["pdf_occurrences"]]
    if value.get("manual_supplement"):
        value["manual_supplement"] = _manual_dto(value["manual_supplement"])
    return value


def _date_evidence_dto(value):
    if not isinstance(value, dict):
        return value
    return {key:item for key,item in value.items() if key in {
        "kind","url","date_kind","match_basis","source_url","field","label","text","source_system",
        "table","record_id","date_basis","report_filename","report_date","note","acquisition_item_id"}}


def _manual_dto(value):
    return {key: item for key, item in value.items() if key in {"generator", "field_sources", "summary_basis", "source_row_sha256"}}


def _calendar_dto(item):
    value={key: value for key, value in item.items() if key not in {"checks", "reader", "attempts", "source_observations", "item_json", "evidence", "original_pdf_base64"}}
    if "pdf_observations" in value:
        value["pdf_observations"]=[_calendar_dto(observation) for observation in value["pdf_observations"]]
    return value


def _occurrence_dto(item):
    value = {key:value for key,value in item.items() if key not in {"checks","reader","attempts","source_observations","original_pdf_base64"}}
    if "publication_date_evidence" in value:
        value["publication_date_evidence"] = _date_evidence_dto(value["publication_date_evidence"])
    return value


def _pdf_dto(item):
    payload = {key: value for key, value in item.items() if key not in {"checks", "reader", "attempts"}}
    if payload.get("manual_enrichment"):
        manual = payload["manual_enrichment"]
        payload["manual_enrichment"] = {key: value for key, value in manual.items() if key in {"generator", "categories", "keywords", "summary_basis", "source_row_sha256"}}
    if "occurrences" in item:
        payload["occurrences"] = [_occurrence_dto(row) for row in item["occurrences"]]
    if isinstance(item.get("latest_occurrence"), dict):
        payload["latest_occurrence"] = _occurrence_dto(item["latest_occurrence"])
    return payload


def _pdf_report_dto(item):
    # Original bytes and arbitrary PDF metadata remain in the external archive.
    fields = {"report_id","document_sha256","source_kind","report_title","filename","report_date",
        "article_count","calendar_count","page_count","monitoring_status","source_label","edition",
        "reporting_period","period_start","period_end","pdf_created_at","pdf_modified_at",
        "imported_at","source_filenames"}
    value = {key: field for key, field in item.items() if key in fields}
    value["articles"] = [_pdf_dto(article) for article in item.get("articles",[])]
    value["calendar_items"] = [_calendar_dto(meeting) for meeting in item.get("calendar_items",[])]
    value["executive_summary"] = []
    value["pages"] = []
    value["report_pdf"] = None  # Git provides metadata, never a PDF download.
    return value
