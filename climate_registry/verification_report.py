"""Private, deterministic T10 inventory; generating it never approves or publishes."""
from __future__ import annotations

import json
import hashlib
from collections import Counter
from pathlib import Path

from climate_delivery.io import atomic_write_bytes, atomic_write_json
from .acquisition_review import digest, now_stamp
from .pdf_summary import ORIGINAL_SUMMARY_LABEL
from .verification import PUBLICATION_POLICY, evaluate_snapshot, canonical_target, review_context, public_snapshot_view

REASONS = {
    "reviewed_summary_receipt_missing": "The additional display summary lacks exact source verification and review receipts; using the verified source summary",
    "no_check": "No verification record",
    "stale_revision": "The verified source revision no longer matches",
    "failed_access": "Source webpage access failed",
    "conflict": "Verification found conflicting facts",
    "partial": "Some fields lack source support",
    "unchecked": "Effective verification is missing",
    "invalid_check_packet": "The verification packet or hash is invalid",
    "invalid_source_binding": "Exact source binding is missing",
    "no_native_binding": "Genuine native article or meeting source binding is missing",
    "incomplete_t1": "T1 did not complete",
    "invalid_reader_evidence": "Complete webpage evidence is missing or its content hash differs",
    "unsupported_fields": "Factual fields lack source support",
    "no_publication": "No approved public version",
    "pending_review": "The new candidate awaits review",
    "owner_hidden": "Hidden by the owner",
    "manual_source_access_unavailable": "The original webpage is unavailable; only the scope supported by independent manual verification is public",
    "manual_packet_invalid": "Invalid manual verification format",
    "manual_source_binding_invalid": "Manual verification source binding differs",
    "manual_run_binding_invalid": "Manual verification run binding differs",
    "manual_review_binding_missing": "Exact candidate independent manual review receipts are missing",
    "manual_review_binding_invalid": "Invalid manual review receipt binding",
    "manual_identity_evidence_missing": "Manual identity or date evidence is missing",
    "unverified_date_observation": "Verified fields may be public; additional unverified webpage dates are hidden",
    "unverified_acquisition_date": "Verified fields may be public; additional unverified acquisition dates are hidden",
    "reader_body_mismatch": "Verified fields may be public; the retained body differs from the verified webpage and is hidden",
    "missing_ineligible_target": "The linked target is missing or lacks effective verification",
    "metadata_only": "Only the approved report/page reference is retained; source facts are hidden"
}


def reason_explanation(code):
    kind, _, field = code.partition(":")
    if kind == "field_outside_verification":
        return "Verified fields may be public; additional unverified fields are hidden: "+field
    if kind == "manual_field_outside_reviewed_scope":
        return "Fields outside the manually verified scope are hidden: "+field
    if kind == "manual_date_outside_evidence":
        return "The original date exceeds manual evidence; only supported dates are shown: "+field
    if kind == "manual_field_value_mismatch":
        return "The manually verified field value differs: "+field
    return REASONS.get(code, code)


def _current_snapshot(connection, kind, identity, targets):
    from .publication import ENTITY_TABLES, _rows
    tables = {table: _rows(connection, table, column + "=?", (identity,)) for table, column in ENTITY_TABLES[kind].items()}
    dependencies = {}
    for prefix, check_kind, table in (("article", "articles", "pdf_intake_article_occurrences"), ("meeting", "meetings", "pdf_intake_calendar_items")):
        selected = [target for target in targets if target["check_kind"] == check_kind]
        ids = {target["occurrence_id"] for target in selected}
        if not ids:
            continue
        pdf_ids = [identity for identity in ids if not identity.startswith("core:")]
        if pdf_ids:
            tables[table] = [row for key in pdf_ids for row in _rows(connection, table, "occurrence_id=?", (key,))]
        attempts = []
        columns = {"occurrence_id": {target["occurrence_id"] for target in selected if target["entity_kind"].startswith("pdf_")},
            "core_article_id" if prefix == "article" else "event_source_id": {
                target["entity_id"] if prefix == "article" else target["occurrence_id"] for target in selected if not target["entity_kind"].startswith("pdf_")}}
        for column, identities in columns.items():
            values = sorted(identities)
            for start in range(0, len(values), 400):
                chunk = values[start:start + 400]
                attempts.extend(_rows(connection, prefix + "_check_attempts", column + " IN (" + ",".join("?" for _ in chunk) + ")", chunk))
        tables[prefix + "_check_attempts"] = attempts
        dependencies[prefix + "_check_runs"] = [row for run in {r["run_id"] for r in attempts}
            for row in _rows(connection, prefix + "_check_runs", "run_id=?", (run,))]
    return {"entity_kind": kind, "entity_id": identity, "tables": tables, "dependencies": dependencies, "information_targets": targets}


def write_verification_report(database, root):
    from .publication import resolve_database, _information_groups, public_revision
    from .read_api import RegistryReader
    database = resolve_database(database)
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1], public=False)
    rows, retained = [], []
    with reader.connect() as connection:
        groups = _information_groups(connection)
        review_proofs = review_context(connection)
        documents = {r["document_sha256"]: dict(r) for r in connection.execute("SELECT document_sha256,filename,date_of_run FROM pdf_intake_documents")}
        publications = { (r["entity_kind"], r["entity_id"]): dict(r) for r in connection.execute("SELECT * FROM registry_publication")}
        keys = set(publications) | set(groups)
        def published(key):
            state = publications.get(key, {})
            sha = state.get("published_candidate_sha256")
            row = connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone() if sha else None
            if not row:
                return None, None
            snapshot = json.loads(row[0])
            if digest(snapshot) != sha:
                raise ValueError("T10 published candidate hash differs")
            review = connection.execute("SELECT status FROM registry_reviews WHERE candidate_sha256=? AND status IN ('pass','accepted_legacy') LIMIT 1", (sha,)).fetchone()
            verdict = evaluate_snapshot(snapshot, reviews=review_proofs.get(sha, ()))
            verdict["approved"] = bool(review)
            verdict["actually_public"] = bool(state.get("is_visible") and review and verdict["eligible"])
            return snapshot, verdict
        for key in sorted(keys):
            state = publications.get(key, {})
            published_snapshot, public = published(key)
            association = False
            target = canonical_target(published_snapshot) if published_snapshot else None
            if public and public["approved"] and not public["actually_public"] and state.get("is_visible") and target:
                _target_snapshot, target_public = published(target)
                association = bool(target_public and target_public["actually_public"])
            current = _current_snapshot(connection, *key, groups.get(key, []))
            latest_sha = state.get("latest_candidate_sha256")
            latest_row = connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (latest_sha,)).fetchone() if latest_sha else None
            snapshots = [("current", current, evaluate_snapshot(current))]
            if latest_row:
                latest = json.loads(latest_row[0])
                if digest(latest) != latest_sha:
                    raise ValueError("T10 latest candidate hash differs")
                snapshots.append(("latest", latest, evaluate_snapshot(latest, reviews=review_proofs.get(latest_sha, ()))))
            if public:
                snapshots.append(("published", published_snapshot, public))
            reviews = [dict(r) for r in connection.execute("SELECT review_id,candidate_sha256,status,reviewed_at,receipt_json FROM registry_reviews WHERE candidate_sha256 IN (?,?) ORDER BY reviewed_at", (latest_sha, state.get("published_candidate_sha256")))]
            for review in reviews:
                receipt = json.loads(review.pop("receipt_json"))
                review["receipt_sha256"] = digest(receipt)
                review["disposition"] = receipt.get("conclusion") or {key: receipt[key] for key in ("basis", "reason", "final_check_error") if key in receipt}
                review["native_session_id"] = (receipt.get("claim") or {}).get("session_id")
            for version, snapshot, verdict in snapshots:
                hidden_fields = public_snapshot_view(snapshot, verdict).get("_hidden_factual_fields", {}) if verdict["eligible"] else {}
                observations = list(verdict["observations"].values()) or [{"occurrence_id": None, "fields": {}, "source_binding": {}, "sources": [], "reasons": ["no_check"], "verification_status": "unchecked"}]
                for observation in observations:
                    manual = observation.get("manual_verification") or {}
                    reasons = list(observation["reasons"]) + manual.get("excluded_fields", []) + hidden_fields.get(observation["occurrence_id"], [])
                    if manual and manual.get("access_status") != "accessible":
                        reasons.append("manual_source_access_unavailable")
                    if not state.get("published_candidate_sha256"):
                        reasons.append("no_publication")
                    if latest_sha != state.get("published_candidate_sha256"):
                        reasons.append("pending_review")
                    if state and not state.get("is_visible"):
                        reasons.append("owner_hidden")
                    if association:
                        reasons.append("metadata_only")
                    elif target and public and not public["actually_public"]:
                        reasons.append("missing_ineligible_target")
                    exact_public_version = version == "published" or version == "latest" and latest_sha == state.get("published_candidate_sha256")
                    public_fact = bool(exact_public_version and public and public["actually_public"] and observation.get("verified"))
                    public_reference = bool(exact_public_version and (association or public and public["actually_public"] and not observation.get("verified")))
                    fields, binding = observation["fields"], observation["source_binding"]
                    rows.append({"version": version, "entity_kind": key[0], "entity_id": key[1], "occurrence_id": observation["occurrence_id"],
                        "title": fields.get("title") or fields.get("anchor_text") or fields.get("name") or key[1],
                        "document_sha256": binding.get("source_document_sha256"), "page": binding.get("page"),
                        "source_report": documents.get(binding.get("source_document_sha256")),
                        "latest_candidate_sha256": latest_sha, "published_candidate_sha256": state.get("published_candidate_sha256"),
                        "is_visible": bool(state.get("is_visible")), "factual_public": public_fact, "entity_has_verified_public_version": bool(public and public["actually_public"]),
                        "public_role": "verified_primary" if public_fact else "metadata_only" if public_reference else "hidden_facts",
                        "canonical_verified_reference": {"entity_kind": target[0], "entity_id": target[1]} if association else None,
                        "verification_status": observation["verification_status"], "access_status": observation.get("access_status", "unchecked"),
                        "collection_status": observation.get("collection_status", "pending"), "sources": observation["sources"],
                        "manual_verification": manual or None,
                        "reason_codes": sorted(set(reasons)), "reason_explanations": [reason_explanation(r) for r in sorted(set(reasons))], "reviews": reviews})
            if public and public["actually_public"] and latest_sha != state.get("published_candidate_sha256"):
                retained.append({"entity_kind": key[0], "entity_id": key[1], "published_candidate_sha256": state["published_candidate_sha256"], "latest_candidate_sha256": latest_sha})
        adoptions = [dict(row) for row in connection.execute("SELECT adoption_id,document_sha256,executive_summary_sha256,adopted_at,adoption_basis FROM pdf_intake_summary_adoptions")] if connection.execute("PRAGMA user_version").fetchone()[0] >= 23 else []
        revision = public_revision(database, connection=connection)
    outcomes = []
    for path in sorted(Path(root).glob("*/acquisition-review/state.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        for outcome in state.get("packet", {}).get("source_outcomes", []):
            outcomes.append({"run": path.parent.parent.name, "source_outcome": outcome, "review_state": state.get("sources", {}).get(outcome.get("source"), {})})
    totals = dict(Counter(row["version"] + ":" + row["entity_kind"] + ":" + row["verification_status"] for row in rows))
    report = {"schema_version": "t10-verification-report.v1", "language": "en", "generated_at": now_stamp(), "database": str(database),
        "publication_policy": PUBLICATION_POLICY, "public_revision": revision, "totals": totals, "rows": rows,
        "source_outcomes": outcomes, "retained_verified_public_versions": retained,
        "original_summary_adoptions": [{**row, "label": ORIGINAL_SUMMARY_LABEL, "fact_verification": "not_asserted"} for row in adoptions]}
    semantic_sha = digest({key: value for key, value in report.items() if key != "generated_at"})
    directory = Path(root) / "registry-review" / "verification-reports"
    latest_path = directory / "latest.json"
    if latest_path.is_file():
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        json_file, human_file = Path(latest["json_path"]), Path(latest["human_report_path"])
        if latest.get("semantic_sha256") == semantic_sha and json_file.is_file() and human_file.is_file():
            previous = json.loads(json_file.read_text(encoding="utf-8"))
            previous_sha = previous.pop("report_sha256", None)
            if (digest(previous) == previous_sha == latest["report_sha256"]
                    and hashlib.sha256(human_file.read_bytes()).hexdigest() == latest.get("human_report_sha256")):
                return {**latest, "reused": True}
    sha = digest(report)
    output = directory / (report["generated_at"].replace(":", "-") + "-" + sha[:12])
    json_path, markdown_path = Path(str(output) + ".json"), Path(str(output) + ".md")
    atomic_write_json(json_path, {**report, "report_sha256": sha})
    def cell(value):
        return str(value or "—").replace("|", r"\|").replace("\n", " ")
    lines = ["# T10 verification and publication inventory", "", "This report records actual states. It does not replace human review or produce a PASS. Original-text adoption does not assert independent fact verification.", "",
        "Versions: current is the current database record; latest is the newest candidate; published is the approved version. An entity may have multiple rows. A pending candidate can coexist with an older verified public version.", "",
        "Historical manual verification is used only within the retained exact independent review receipts and scope; external originals were not reread for this report.", "",
        "Generated: " + report["generated_at"], "Database: " + str(database), "Policy: " + PUBLICATION_POLICY, "", "## Complete item inventory", "",
        "|Version|Type / identity|Title|Source / report page|Verification|Actual public role|Reason / review|", "|---|---|---|---|---|---|---|"]
    for row in rows:
        sources = "; ".join(s["source_url"] + ("; " + str(s.get("error")) if s.get("error") else "") for s in row["sources"])
        source = sources + ("; " + str((row["source_report"] or {}).get("filename") or "PDF") + " " + str(row["document_sha256"]) + " page " + str(row["page"]) if row["document_sha256"] else "")
        disposition = "; ".join(r["status"] + ": " + json.dumps(r["disposition"], ensure_ascii=False) for r in row["reviews"])
        lines.append("|" + "|".join(cell(x) for x in (row["version"],row["entity_kind"]+" / "+row["entity_id"], row["title"], source,
            row["verification_status"]+" / "+row["access_status"], row["public_role"], "; ".join(reason_explanation(reason)+" ("+reason+")" for reason in row["reason_codes"])+"; "+disposition)) + "|")
    lines.extend(["", "## Failed and zero-candidate sources", "", *["- " + json.dumps(row, ensure_ascii=False) for row in outcomes],
        "", "## Retained older verified public versions", "", *["- " + json.dumps(row, ensure_ascii=False) for row in retained],
        "", "## Original-text adoption (not fact verification)", "", *["- " + json.dumps(row, ensure_ascii=False) for row in report["original_summary_adoptions"]]])
    human_bytes = ("\n".join(lines)+"\n").encode()
    atomic_write_bytes(markdown_path, human_bytes)
    receipt = {"status": "written", "json_path": str(json_path), "human_report_path": str(markdown_path), "report_sha256": sha,
        "totals": totals, "row_count": len(rows), "source_outcome_count": len(outcomes),
        "semantic_sha256": semantic_sha, "human_report_sha256": hashlib.sha256(human_bytes).hexdigest(), "reused": False}
    atomic_write_json(output.parent / "latest.json", receipt)
    return receipt
