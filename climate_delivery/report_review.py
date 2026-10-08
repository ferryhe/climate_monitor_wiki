"""Frozen biweekly reports, independent native PDF review and exact-file delivery."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from climate_registry.acquisition_review import (
    digest, timestamp, now_stamp, claim_review, validate_claim, read_evidence, read_verified_text, freeze_readable, knowledge_fields,
)
from climate_registry.range_reports import (
    _range_source, _merge_range_sources, _meeting_payload, _pdf_calendar_payload,
    _freeze_executive_summary,
)
from .io import atomic_write_json, atomic_write_bytes, transaction_lock as exclusive_lock
from .templates import rendering_metadata
from .templates.adapters import adapt_range_report
from .templates.iaa_csc import render_report
from .templates.model import Report, Update, Citation

ET = ZoneInfo("America/New_York")


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _root(root, occurrence):
    day = date.fromisoformat(occurrence)
    if day.isoformat() != occurrence:
        raise ValueError("invalid report occurrence")
    return Path(root) / "reports" / occurrence


def _material_versions(reader, allowed_refs=None):
    with reader.connect() as connection:
        result = [dict(row) for row in connection.execute("SELECT * FROM knowledge_versions ORDER BY rowid")
            if allowed_refs is None or row["source_ref"] in allowed_refs] if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='knowledge_versions'").fetchone() else []
        # Read verified historical transaction timestamps without modifying Registry.
        # Date-only/absent values remain unknown, never the migration/check date.
        known = {row["source_ref"] for row in result}
        if reader._has_pdf_intake(connection):
            rows = list(connection.execute("""SELECT 'article' AS kind,o.occurrence_id,p.article_id AS entity_id,
                p.imported_at,o.occurrence_json AS fields,d.document_sha256,o.page,'historical_pdf_article_imported_at' AS basis
                FROM pdf_intake_article_occurrences o JOIN pdf_intake_articles p ON p.article_id=o.article_id
                JOIN pdf_intake_documents d ON d.document_sha256=o.source_document_sha256"""))
            rows += list(connection.execute("""SELECT 'meeting' AS kind,o.occurrence_id,o.event_id AS entity_id,
                d.imported_at,o.item_json AS fields,d.document_sha256,o.page,'historical_pdf_source_commit' AS basis
                FROM pdf_intake_calendar_items o JOIN pdf_intake_documents d ON d.document_sha256=o.source_document_sha256"""))
            for row in rows:
                if row["occurrence_id"] in known or allowed_refs is not None and row["occurrence_id"] not in allowed_refs:
                    continue
                try:
                    timestamp(row["imported_at"])
                except (ValueError, TypeError):
                    continue
                raw = json.loads(row["fields"])
                if row["kind"] == "article":
                    fields = {key: raw.get(key) for key in ("title", "anchor_text", "summary", "publication_date")}
                else:
                    from climate_monitor.meeting_fields import pdf_meeting_fields, MEETING_FIELDS
                    normalized = pdf_meeting_fields(raw)
                    fields = {key: normalized.get(key) for key in MEETING_FIELDS}
                material = digest(knowledge_fields(fields))
                result.append({"knowledge_id": digest([row["kind"], row["entity_id"], row["occurrence_id"], material, row["imported_at"]]),
                    "entity_kind": row["kind"], "entity_id": row["entity_id"], "source_kind": "pdf", "source_ref": row["occurrence_id"],
                    "material_sha256": material, "fields_json": json.dumps(fields),
                    "evidence_json": json.dumps({"document_sha256": row["document_sha256"], "page": row["page"], "imported_at": row["imported_at"]}),
                    "first_ingested_at": row["imported_at"], "substantive_updated_at": None,
                    "recorded_at": row["imported_at"], "time_basis": row["basis"]})
        # Accepted historical native events may predate the knowledge ledger.
        # Their persisted event/version times remain evidence, never migration time.
        if reader.public and connection.execute("PRAGMA user_version").fetchone()[0]>=21:
            from climate_monitor.meeting_fields import MEETING_FIELDS
            semantic=lambda row:{key:row.get(key) for key in MEETING_FIELDS if key not in {"raw_date","source_urls"}}
            for row in connection.execute("""SELECT s.*,e.created_at,e.record_version,ai.discovery_kind FROM climate_event_sources s
                JOIN climate_events e ON e.event_id=s.event_id
                LEFT JOIN meeting_run_items ri ON ri.meeting_run_id=s.meeting_run_id AND ri.article_id=s.article_id AND ri.content_version_id=s.content_version_id
                LEFT JOIN acquisition_items ai ON ai.acquisition_item_id=ri.acquisition_item_id"""):
                if row["event_source_id"] in known or allowed_refs is not None and row["event_source_id"] not in allowed_refs:
                    continue
                first,changed,recorded=None,None,None
                try:
                    timestamp(row["created_at"])
                    first=row["created_at"]
                except (ValueError,TypeError):
                    pass
                previous=None
                versions=list(connection.execute("SELECT * FROM climate_event_versions WHERE event_id=? ORDER BY record_version",(row["event_id"],)))
                for version in versions:
                    fields=semantic(json.loads(version["state_json"]))
                    try:
                        timestamp(version["recorded_at"])
                        recorded=version["recorded_at"]
                    except (ValueError,TypeError):
                        recorded=None
                    if previous is not None and fields!=previous:
                        changed=recorded
                    previous=fields
                if not versions or not (first or changed):
                    continue
                material=digest(knowledge_fields(fields))
                result.append({"knowledge_id":digest(["meeting",row["event_id"],row["event_source_id"],material,changed or first]),
                    "entity_kind":"meeting","entity_id":row["event_id"],"source_kind":row["discovery_kind"] or "site","source_ref":row["event_source_id"],
                    "material_sha256":material,"fields_json":json.dumps(fields),"evidence_json":json.dumps({
                        "event_version":row["record_version"],"event_version_sha256":versions[-1]["state_sha256"],
                        **{key:row[key] for key in ("event_source_id","meeting_run_id","content_version_id","content_sha256","candidate_sha256","source_url")}}),
                    "first_ingested_at":first,"substantive_updated_at":changed,"recorded_at":recorded or changed or first,
                    "time_basis":"historical_native_event_created_at" if first else "legacy_time_unknown"})
    latest = {}
    for row in result:
        latest[(row["entity_kind"], row["entity_id"], row["source_ref"])] = row
    return list(latest.values())


def freeze_biweekly(reader, root: Path, *, occurrence: str, web_reader=None, pdf_reader=None,
    manifest=None, generated_at=None):
    # Existing frozen reports remain readable without reopening their old Registry.
    state_path=_root(root,occurrence)/"state.json"
    if state_path.exists():
        return json.loads(state_path.read_text())
    with reader.public_snapshot():
        with reader.connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0]>=21:
                web_reader=pdf_reader=None
                manifest=None
        return _freeze_biweekly(reader,root,occurrence=occurrence,web_reader=web_reader,pdf_reader=pdf_reader,manifest=manifest,generated_at=generated_at)


def _freeze_biweekly(reader, root: Path, *, occurrence: str, web_reader=None, pdf_reader=None,
    manifest=None, generated_at=None):
    """Read-only knowledge selection. Report state alone is written here."""
    day = date.fromisoformat(occurrence)
    end = datetime.combine(day, datetime.min.time(), ET)
    start = datetime.combine(day - timedelta(days=14), datetime.min.time(), ET)
    generated = generated_at or datetime.now(timezone.utc)
    report_root = _root(root, occurrence)
    with exclusive_lock(report_root, "report"):
        if (report_root / "state.json").exists():
            return json.loads((report_root / "state.json").read_text())
        manifest = manifest or {"web_items": [], "pdf_occurrence_ids": [], "pdf_calendar_occurrence_ids": []}
        web_ids = {item["acquisition_item_id"] for item in manifest["web_items"]}
        pdf_ids = set(manifest["pdf_occurrence_ids"])
        calendar_ids = set(manifest.get("pdf_calendar_occurrence_ids", []))
        meeting = _meeting_payload(reader, None, base_date=day.isoformat())
        with reader.connect() as connection:
            public_projection=reader.public and connection.execute("PRAGMA user_version").fetchone()[0]>=21
            if public_projection:
                # Approved acquisition bodies can precede the legacy current pointer.
                public_refs=set()
                for (identity,) in connection.execute("SELECT article_id FROM articles"):
                    detail=reader.article(identity)
                    content=detail.get("available_content") or detail.get("content") or {}
                    version=content.get("content_version_id")
                    if version:
                        public_refs.update(row[0] for row in connection.execute("""SELECT acquisition_item_id FROM acquisition_items
                            WHERE article_id=? AND content_version_id=?""",(identity,version)))
            else:
                public_refs = {row[0] for row in connection.execute("""SELECT i.acquisition_item_id FROM acquisition_items i
                    JOIN articles a ON a.article_id=i.article_id WHERE a.current_content_version_id=i.content_version_id""")}
            if reader._has_pdf_intake(connection):
                public_refs |= {row[0] for row in connection.execute("SELECT occurrence_id FROM pdf_intake_article_occurrences")}
                public_refs |= {row[0] for row in connection.execute("SELECT occurrence_id FROM pdf_intake_calendar_items")}
            public_refs |= {item["event_source_id"] for event in meeting["records"] for item in event.get("sources",[]) if item.get("is_current")}
        versions = _material_versions(reader, public_refs)
        source = _range_source(reader, "0001-01-01", "9999-12-31", acquisition_item_ids=public_refs)
        if web_reader:
            versions += _material_versions(web_reader, web_ids)
            web_source = _range_source(web_reader, "0001-01-01", "9999-12-31",
                acquisition_item_ids=web_ids, pdf_occurrence_ids=set())
            display = {item["article_id"]: item.get("review", {}).get("display", {}) for item in manifest["web_items"]}
            for article in web_source["articles"]:
                article.update(display.get(article["article_id"], {}))
            source = _merge_range_sources(source, web_source)
        if pdf_reader:
            versions += _material_versions(pdf_reader, pdf_ids | calendar_ids)
            source = _merge_range_sources(source, _range_source(pdf_reader, "0001-01-01", "9999-12-31",
                acquisition_item_ids=set(), pdf_occurrence_ids=pdf_ids), preferred_acquisition_item_ids=web_ids)
        appeared = set()
        for archived in (Path(root) / "reports").glob("*/archive.json"):
            receipt = json.loads(archived.read_text())
            appeared.update(tuple(value) for value in receipt["material_versions"])
        # Frozen information cannot be selected again merely because review/send
        # has not completed. Archive receipts also cover historical snapshots.
        for frozen in (Path(root) / "reports").glob("*/snapshot.json"):
            snapshot = json.loads(frozen.read_text())
            appeared.update((v["entity_kind"], v["entity_id"], v["material_sha256"],
                v["substantive_updated_at"] or v["first_ingested_at"]) for v in snapshot.get("material_versions", []))
        approved = {item["acquisition_item_id"]: item["review"] for item in manifest["web_items"]
            if item.get("review", {}).get("status") == "pass"}
        with reader.connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0]>=21:
                approvals={(row[0],row[1]):row[2] for row in connection.execute("SELECT * FROM approved_public_reviews")}
                approved={}
                for row in connection.execute("SELECT entity_kind,entity_id,snapshot_json FROM public_snapshot_metadata"):
                    at=approvals.get((row[0],row[1]))
                    if at:
                        for value in json.loads(row[2])["tables"].get("knowledge_versions",[]):
                            approved[value["source_ref"]]={"status":"pass","approved_at":at}
        eligible = {}
        known_refs = {value["source_ref"] for value in versions}
        gaps = [{"source_ref": ref, "reason": "legacy_ingestion_time_unknown"}
            for ref in sorted((public_refs | web_ids | pdf_ids | calendar_ids) - known_refs)]
        for version in versions:
            material_id = (version["entity_kind"], version["entity_id"], version["material_sha256"],
                version["substantive_updated_at"] or version["first_ingested_at"])
            if material_id in appeared:
                continue
            changed = version["substantive_updated_at"] or version["first_ingested_at"]
            if not changed:
                gaps.append({"source_ref": version["source_ref"], "reason": "legacy_ingestion_time_unknown"})
                continue
            value = timestamp(changed)
            # Eligibility is first ingestion OR a substantive update. A newer
            # verified version after the cutoff must not erase an in-window first.
            first = version["first_ingested_at"]
            if not start <= value < end and first and start <= timestamp(first) < end:
                changed, value = first, timestamp(first)
            reason = None
            if start <= value < end:
                reason = "substantive_update" if changed == version["substantive_updated_at"] else "first_ingested"
            review = approved.get(version["source_ref"], {})
            from climate_monitor.schedule import ANCHOR
            intended_day = ANCHOR + timedelta(days=((value.astimezone(ET).date() - ANCHOR).days // 14 + 1) * 14)
            intended_end = datetime.combine(intended_day, datetime.min.time(), ET)
            if value < start and review.get("approved_at") and timestamp(review["approved_at"]) <= generated:
                reason = ("late_review_carryforward" if timestamp(review["approved_at"]) >= intended_end
                    else "delayed_activation_carryforward")
            if reason:
                eligible[version["source_ref"]] = {**version, "selection_reason": reason,
                    "original_period_time": changed, "intended_report_date": intended_day.isoformat(), "approved_at": review.get("approved_at")}
        def selected(item):
            # Article observations carry an ID; PDF document provenance does not.
            # The PDF update itself carries the occurrence ID used by material versions.
            refs = [o.get("observation_id") for o in item.get("source_observations", [])]
            refs += [item.get("observation_id"), item.get("occurrence_id")]
            return [eligible[ref] for ref in dict.fromkeys(refs) if ref in eligible]
        def material_item(item, material):
            result = {**item, "material_versions": material}
            if public_projection:
                from climate_registry.publication import snapshot_metadata
                kind,identity=("article",item["article_id"]) if item.get("article_id") else ("pdf_article",item["pdf_article_id"])
                detail=reader.article(identity) if kind=="article" else reader.pdf_article(identity)
                occurrences={value["occurrence_id"]:value for value in detail.get("pdf_occurrences" if kind=="article" else "occurrences",[])}
                with reader.connect() as connection:
                    snapshot=snapshot_metadata(connection,kind,identity)
                selected_summaries=[]
                for version in material:
                    if version["source_kind"]!="information_check" or snapshot is None:
                        continue
                    occurrence=occurrences.get(version["source_ref"],{})
                    information=occurrence.get("verified_information") or {}
                    fields=json.loads(version["fields_json"])
                    evidence=json.loads(version["evidence_json"])
                    check=next((row for row in snapshot["tables"].get("article_check_attempts",[]) if
                        row["occurrence_id"]==version["source_ref"] and row["run_id"]==evidence.get("run_id") and
                        row["packet_sha256"]==evidence.get("packet_sha256") and row["verification_status"]=="verified"),None)
                    if not check or not information.get("summary") or knowledge_fields({"summary":information["summary"]})["summary"]!=fields.get("summary"):
                        continue
                    packet=json.loads(check["packet_json"])
                    if digest(packet)!=check["packet_sha256"] or packet.get("verified_information")!=information or check["source_url"]!=evidence.get("source_url"):
                        continue
                    selected_summaries.append({"summary":information["summary"],"provenance":{
                        "basis":"approved_source_verification","candidate_sha256":digest(snapshot),
                        "occurrence_id":version["source_ref"],"dto_field":("pdf_occurrences" if kind=="article" else "occurrences")+"[occurrence_id="+version["source_ref"]+"].verified_information.summary",
                        "source_url":information["source_url"],"body_sha256":information["body_sha256"],"generated_at":information["generated_at"],
                        "knowledge_id":version["knowledge_id"],"material_sha256":version["material_sha256"],
                        "original_period_time":version["original_period_time"],"run_id":check["run_id"],"packet_sha256":check["packet_sha256"]}})
                if selected_summaries:
                    result["selected_material_summaries"]=selected_summaries
                return result
            # Historical schemas used the selected PDF summary as their primary.
            # Schema21 keeps canonical display and selected-source text distinct.
            summaries = [json.loads(version["fields_json"]).get("summary") for version in material
                if version["source_kind"] in {"pdf", "information_check"}]
            summaries = list(dict.fromkeys(value for value in summaries if value))
            if summaries:
                result["summary"] = "\n\n".join(summaries)
            return result
        articles = []
        for item in source["articles"]:
            material = selected(item)
            if material:
                articles.append(material_item(item, material))
        pdf_updates = [material_item(item, selected(item)) for item in source["pdf_source_updates"] if selected(item)]
        # Calendar is information, not a way to refill the report with unchanged meetings.
        meeting_records=[]
        for item in meeting["records"]:
            refs={source["event_source_id"] for source in item.get("sources",[]) if source.get("is_current")}
            material=[value for ref,value in eligible.items() if ref in refs and value["entity_kind"]=="meeting" and value["entity_id"]==item["event_id"]]
            if material:
                record={**item,"material_versions":material}
                if public_projection:
                    from climate_registry.publication import snapshot_metadata
                    with reader.connect() as connection:
                        approved=snapshot_metadata(connection,"meeting",item["event_id"])
                    record["material_provenance"]={"basis":"approved_public_version","candidate_sha256":digest(approved)}
                meeting_records.append(record)
        meeting["records"]=meeting_records
        calendar = _pdf_calendar_payload(reader, base_date=day.isoformat(), overlay_reader=pdf_reader,
            activated_pdf_occurrence_ids=pdf_ids if pdf_reader else None,
            activated_calendar_ids=calendar_ids if pdf_reader else None)
        calendar["records"] = [item for item in calendar["records"] if item.get("occurrence_id") in eligible]
        represented_refs = {value["source_ref"] for article in articles + pdf_updates for value in article["material_versions"]}
        represented_refs |= {value["source_ref"] for item in meeting["records"] for value in item["material_versions"]}
        represented_refs |= {item["occurrence_id"] for item in calendar["records"]}
        snapshot = {"schema_version": "climate-biweekly-report.v1", "occurrence": occurrence,
            "date_range": {"start": (day - timedelta(days=14)).isoformat(), "end": (day - timedelta(days=1)).isoformat(), "inclusive": True},
            "selection_window": {"start": start.astimezone(timezone.utc).isoformat(), "end": end.astimezone(timezone.utc).isoformat(), "end_exclusive": True},
            "timezone": "America/New_York", "articles": articles, "pdf_source_updates": pdf_updates,
            "executive_summary": _freeze_executive_summary({**source, "articles": articles, "pdf_source_updates": pdf_updates}),
            "pdf_source_exclusion_counts": source["pdf_source_exclusion_counts"],
            "unknown_publication_date_count": source["unknown_publication_date_count"],
            "unknown_publication_date_article_ids": source["unknown_publication_date_article_ids"],
            "meeting": meeting, "pdf_calendar": calendar, "coverage_gaps": gaps,
            "material_versions": [value for ref, value in eligible.items() if ref in represented_refs], "active_manifest": manifest,
            "registry_sha256": {"public": hashlib.sha256((getattr(reader,"_snapshot_source_connection",None) or reader._snapshot_connection).serialize()).hexdigest(),
                "web": file_sha(web_reader.database) if web_reader else None,
                "pdf": file_sha(pdf_reader.database) if pdf_reader else None}, "created_at": generated.isoformat()}
        snapshot["snapshot_sha256"] = digest(snapshot)
        snapshot["snapshot_id"] = "biweekly-" + snapshot["snapshot_sha256"][:24]
        atomic_write_json(report_root / "snapshot.json", snapshot)
        state = {"occurrence": occurrence, "snapshot_sha256": snapshot["snapshot_sha256"], "revision": 0,
            "status": "no_eligible_information", "history": [], "updated_at": now_stamp()}
        atomic_write_json(report_root / "state.json", state)
        if articles or pdf_updates or meeting["records"] or calendar["records"]:
            state = _new_revision(report_root, state, asdict(adapt_range_report(snapshot)), parent_context=None)
        return state


def _display_report(source):
    value = copy.deepcopy(source)
    value["updates"] = tuple(Update(**{**item,
        "citations": tuple(Citation(**c) for c in item["citations"]),
        "paragraphs": tuple(item["paragraphs"]), "metadata": tuple(tuple(pair) for pair in item["metadata"]),
        "coverage_period": tuple(item["coverage_period"]) if item.get("coverage_period") else None}) for item in value["updates"])
    for field in Report.__dataclass_fields__:
        if field in value and isinstance(value[field], list):
            value[field] = tuple(tuple(v) if isinstance(v, list) else v for v in value[field])
    value["summary_citations"] = tuple(Citation(**c) for c in value["summary_citations"])
    return Report(**value)


def _new_revision(root, state, source, *, parent_context):
    number = state["revision"] + 1
    revision_root = root / "revisions" / f"{number:04d}"
    # If render/extraction fails this identity is retained, never overwritten.
    if revision_root.exists():
        number = max(int(path.name) for path in (root / "revisions").iterdir() if path.name.isdecimal()) + 1
        revision_root = root / "revisions" / f"{number:04d}"
    revision_root.mkdir(parents=True)
    atomic_write_json(revision_root / "report-source.json", source)
    try:
        render_report(_display_report(source), revision_root / "report.pdf")
        pdf = PdfReader(revision_root / "report.pdf")
        text = "\n\n".join(page.extract_text() or "" for page in pdf.pages)
        if not text.strip():
            raise ValueError("rendered PDF has no reviewable text")
        atomic_write_bytes(revision_root / "full-text.txt", text.encode())
        subprocess.run(["pdftoppm", "-png", "-r", "100", str(revision_root / "report.pdf"),
            str(revision_root / "page")], check=True, capture_output=True)
        pages = sorted(revision_root.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
        if len(pages) != len(pdf.pages):
            raise ValueError("PDF page render is incomplete")
        packet = {"schema_version": "climate-pdf-review.v1", "occurrence": state["occurrence"],
            "revision": number, "snapshot_sha256": state["snapshot_sha256"],
            "source_sha256": file_sha(revision_root / "report-source.json"), "renderer": rendering_metadata(),
            "pdf_path": "report.pdf", "pdf_sha256": file_sha(revision_root / "report.pdf"),
            "text_path": "full-text.txt", "text_sha256": file_sha(revision_root / "full-text.txt"),
            "readable_text": freeze_readable(revision_root / "full-text.readable.json", text),
            "pages": [{"page": index, "path": path.name, "sha256": file_sha(path)} for index, path in enumerate(pages, 1)],
            "parent_context": parent_context, "created_at": now_stamp()}
        atomic_write_json(revision_root / "packet.json", packet)
        state.update(revision=number, status="pending_review", packet=packet, updated_at=now_stamp())
    except Exception as exc:
        state.update(revision=number, status="generation_failed", error=f"{type(exc).__name__}: {exc}", updated_at=now_stamp())
        atomic_write_json(revision_root / "failure.json", {"error": state["error"], "created_at": now_stamp()})
        atomic_write_json(root / "state.json", state)
        raise
    atomic_write_json(root / "state.json", state)
    return state


def _verify_packet(root, state):
    packet = state["packet"]
    revision_root = root / "revisions" / f"{state['revision']:04d}"
    snapshot = json.loads((root / "snapshot.json").read_text())
    raw = {key: value for key, value in snapshot.items() if key not in {"snapshot_id", "snapshot_sha256"}}
    if digest(raw) != state["snapshot_sha256"] or packet["snapshot_sha256"] != state["snapshot_sha256"]:
        raise ValueError("frozen snapshot identity changed")
    for field, filename in (("source_sha256", "report-source.json"), ("pdf_sha256", "report.pdf"), ("text_sha256", "full-text.txt")):
        if file_sha(revision_root / filename) != packet[field]:
            raise ValueError("report revision artifact changed: " + filename)
    for page in packet["pages"]:
        path = (revision_root / page["path"]).resolve()
        if path.parent != revision_root.resolve() or file_sha(path) != page["sha256"]:
            raise ValueError("review page identity changed")
    if packet["revision"] != state["revision"] or packet["occurrence"] != state["occurrence"] or json.loads((revision_root / "packet.json").read_text()) != packet:
        raise ValueError("review packet identity changed")
    return revision_root, packet


def claim_report(root: Path, occurrence: str, **context):
    report_root = _root(root, occurrence)
    with exclusive_lock(report_root, "report"):
        state = json.loads((report_root / "state.json").read_text())
        if state["status"] != "pending_review":
            raise ValueError("no pending PDF revision")
        revision_root, packet = _verify_packet(report_root, state)
        if packet["parent_context"] == context["session_id"]:
            raise ValueError("modified PDF requires the next independent cron context")
        return claim_review(revision_root, packet, **context)


def regenerate_report(root: Path, occurrence: str, *, reason: str):
    """An operator rerenders after a supported correction or deployed code fix."""
    report_root = _root(root, occurrence)
    with exclusive_lock(report_root, "report"):
        state = json.loads((report_root / "state.json").read_text())
        if not reason or state["status"] not in {"blocked_code_change", "review_failed", "generation_failed"}:
            raise ValueError("regeneration requires a retained blocker and explicit repair reason")
        revision_root = report_root / "revisions" / f"{state['revision']:04d}"
        source = json.loads((revision_root / "report-source.json").read_text())
        previous = json.loads((revision_root / "review.json").read_text()) if (revision_root / "review.json").exists() else {}
        state["history"].append({"revision": state["revision"], "regeneration_reason": reason,
            "renderer": rendering_metadata(), "recorded_at": now_stamp()})
        return _new_revision(report_root, state, source, parent_context=previous.get("claim", {}).get("session_id"))


def submit_report_review(root: Path, occurrence: str, token: str, *, status: str, reason: str,
    changes=None, proposal=None, now=None):
    report_root = _root(root, occurrence)
    with exclusive_lock(report_root, "report"):
        state = json.loads((report_root / "state.json").read_text())
        if state["status"] != "pending_review" or not reason or status not in {"pass", "changes_requested", "review_failed", "blocked_code_change"}:
            raise ValueError("invalid PDF review result")
        revision_root, packet = _verify_packet(report_root, state)
        claim, events = validate_claim(revision_root, packet, token, now=now)
        inspections = []
        if status in {"pass", "changes_requested", "blocked_code_change"}:
            text_path = revision_root / packet["text_path"]
            inspections.append(read_verified_text(events, text_path, text_path.read_text(), packet.get("readable_text")))
            inspections.extend(read_evidence(events, revision_root / page["path"], image=True) for page in packet["pages"])
        receipt = {"status": status, "reason": reason, "claim": claim, "packet": packet,
            "inspections": inspections, "inspection_sha256": digest(inspections),
            "approved_at": (now or datetime.now(timezone.utc)).isoformat() if status == "pass" else None,
            "changes": changes, "proposal": proposal}
        if status == "blocked_code_change" and not (isinstance(proposal, dict) and all(proposal.get(key) for key in
            ("reason", "pdf_evidence", "root_cause", "files", "suggested_change", "pr_title", "pr_description", "validation"))):
            raise ValueError("code blocker requires evidence and a concrete PR proposal")
        source = None
        if status == "changes_requested":
            source = json.loads((revision_root / "report-source.json").read_text())
            if not isinstance(changes, dict) or not changes or set(changes) - {"title", "executive_summary", "updates"}:
                raise ValueError("only supported derived display changes can create a revision")
            if "title" in changes and (not isinstance(changes["title"], str) or not changes["title"].strip()):
                raise ValueError("report title must be nonempty text")
            if "executive_summary" in changes and (not isinstance(changes["executive_summary"], list)
                or not all(isinstance(value, str) and value.strip() for value in changes["executive_summary"])):
                raise ValueError("executive summary must contain text paragraphs")
            edits = changes.get("updates", {})
            if not isinstance(edits, dict):
                raise ValueError("updates must map frozen ordinals to display edits")
            for index, edit in edits.items():
                if (not str(index).isdecimal() or int(index) >= len(source["updates"])
                    or not isinstance(edit, dict) or not edit or set(edit) - {"title", "paragraphs"}
                    or "title" in edit and (not isinstance(edit["title"], str) or not edit["title"].strip())
                    or "paragraphs" in edit and (not isinstance(edit["paragraphs"], list)
                        or not all(isinstance(value, str) and value.strip() for value in edit["paragraphs"]))):
                    raise ValueError("update evidence identity cannot be modified")
                source["updates"][int(index)].update(edit)
            source.update({key: value for key, value in changes.items() if key != "updates"})
        atomic_write_json(revision_root / "review.json", receipt)
        atomic_write_json(revision_root / "claim.json", {**claim, "released_at": now_stamp()})
        state["history"].append({"revision": state["revision"], "review": receipt})
        if status == "pass":
            state.update(status="approved", approval=receipt)
            snapshot = json.loads((report_root / "snapshot.json").read_text())
            atomic_write_json(report_root / "archive.json", {"occurrence": occurrence, "revision": state["revision"],
                "pdf_sha256": packet["pdf_sha256"], "snapshot_sha256": packet["snapshot_sha256"],
                "material_versions": sorted({(v["entity_kind"], v["entity_id"], v["material_sha256"],
                    v["substantive_updated_at"] or v["first_ingested_at"]) for v in snapshot["material_versions"]}),
                "approved_at": receipt["approved_at"]})
        elif status == "changes_requested":
            return _new_revision(report_root, state, source, parent_context=claim["session_id"])
        else:
            state.update(status=status, blocker=proposal, reason=reason)
        state["updated_at"] = now_stamp()
        atomic_write_json(report_root / "state.json", state)
        return state


def send_approved(root: Path, occurrence: str, *, no_send=True, config_path=None,
    now=None, smtp_factory=None):
    """No render/freeze/acquisition calls. No-send never reads SMTP config."""
    report_root = _root(root, occurrence)
    with exclusive_lock(report_root, "report"):
        state = json.loads((report_root / "state.json").read_text())
        if state["status"] != "approved":
            raise ValueError("report is not approved")
        revision_root, packet = _verify_packet(report_root, state)
        approval = state["approval"]
        if approval["status"] != "pass" or approval["packet"] != packet or not approval["inspections"] or approval["inspection_sha256"] != digest(approval["inspections"]):
            raise ValueError("approval does not match the exact PDF revision")
        if (now or datetime.now(timezone.utc)) < timestamp(approval["approved_at"]) + timedelta(minutes=60):
            raise ValueError("final approval must be at least 60 minutes old")
        if no_send:
            receipt = {"status": "validated_no_send", "revision": state["revision"], "pdf_sha256": packet["pdf_sha256"], "observed_at": now_stamp()}
            atomic_write_json(report_root / "no-send.json", receipt)
            return receipt
        from .config import load_delivery_config
        from .delivery import deliver
        mail = report_root / "mail"
        # A changed revision may not restart an occurrence with an earlier delivery.
        for path in mail.glob("*.json"):
            old = json.loads(path.read_text())
            if old.get("report_sha256") != packet["source_sha256"] and any(
                item["status"] in {"sending", "unknown", "sent"} for item in old.get("recipients", {}).values()):
                raise ValueError("this occurrence already has sent or ambiguous recipients")
        config = load_delivery_config(config_path)
        if len(config.recipients) != 4:
            raise ValueError("the retained four-recipient delivery config is required")
        source = json.loads((revision_root / "report-source.json").read_text())
        urls = list(dict.fromkeys(c["url"] for item in source["updates"] for c in item["citations"] if c.get("url")))
        # Calendar-only editions still cite their frozen original source URLs.
        import re
        urls = list(dict.fromkeys(urls + [url for row in source["key_dates"]
            for url in re.findall(r"https?://[^\s]+", row[-1])]))
        summary = {"schema_version": 1, "report": {"date": occurrence, "title": source["title"], "sha256": packet["source_sha256"]},
            "executive_summary": source["executive_summary"], "highlights": [
                {"pillar": "A", "title": item["title"], "summary": "\n\n".join(item["paragraphs"]),
                 "url": next(c["url"] for c in item["citations"] if c.get("url"))}
                for item in source["updates"] if any(c.get("url") for c in item["citations"])], "original_links": urls}
        try:
            result = deliver(summary, revision_root / "report.pdf", config, mail, smtp_factory=smtp_factory,
                allow_empty_links=True,  # Approved PDF-only evidence need not invent an HTTP source.
                clock=(lambda: now) if now else None)
        except Exception as exc:
            atomic_write_json(report_root / "delivery.json", {"status": "failed_or_ambiguous", "error": type(exc).__name__,
                "revision": state["revision"], "observed_at": now_stamp()})
            raise
        atomic_write_json(report_root / "delivery.json", {**result, "revision": state["revision"], "pdf_sha256": packet["pdf_sha256"], "observed_at": now_stamp()})
        return result


def report_states(root: Path, *, public=False):
    result = []
    for path in sorted((Path(root) / "reports").glob("*/state.json"), reverse=True):
        state = json.loads(path.read_text())
        if public:
            if state["status"] != "approved":
                continue
            _, packet = _verify_packet(path.parent, state)
            state = {"occurrence": state["occurrence"], "revision": state["revision"], "status": "approved",
                "pdf_sha256": packet["pdf_sha256"], "approved_at": state["approval"]["approved_at"]}
        else:
            revision_root = path.parent / "revisions" / f"{state['revision']:04d}"
            claim_path = revision_root / "claim.json"
            if claim_path.is_file():
                claim = json.loads(claim_path.read_text())
                state["claim"] = claim
                state["claim_state"] = "released" if claim.get("released_at") else "expired" if timestamp(claim["deadline"]) <= datetime.now(timezone.utc) else "owned"
            state["claim_history"] = [json.loads(p.read_text()) for p in sorted((revision_root / "claim-history").glob("*.json"))]
            state["recipient_receipts"] = [json.loads(p.read_text()) for p in sorted((path.parent / "mail").glob("*.json"))]
            for name in ("delivery", "no-send"):
                saved = path.parent / (name + ".json")
                if saved.exists():
                    state[name] = json.loads(saved.read_text())
        result.append(state)
    return result
