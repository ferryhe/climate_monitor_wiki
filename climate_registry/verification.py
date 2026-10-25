"""Frozen source-bound verification for public facts and T10 diagnostics."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

from .acquisition_review import digest
from climate_monitor.meeting_fields import MEETING_FIELDS
from .information_checks import _sha, _check_state, attempt_identity, source_fields, source_revision, verified_packet_from_attempt

PUBLICATION_POLICY = "verified-primary-original-adoption.v1"


def _native_binding(snapshot, target):
    tables = snapshot.get("tables", {})
    binding = target.get("source_binding") or {}
    if target["check_kind"] == "meetings":
        event = next((r for r in tables.get("climate_events", []) if r["event_id"] == target["entity_id"]), None)
        source = next((r for r in tables.get("climate_event_sources", []) if r["event_source_id"] == target["occurrence_id"]), None)
        if not event or not source:
            return False
        actual = {"event_id": event["event_id"], "record_version": event["record_version"],
            **{k: source[k] for k in ("event_source_id", "content_version_id", "content_sha256", "candidate_sha256", "source_url")}}
        fields = source_fields("meetings", dict(event, source_urls=[source["source_url"]]))
        return actual == binding and fields == target["fields"]
    article = next((r for r in tables.get("articles", []) if r["article_id"] == target["entity_id"]), None)
    if not article or any(binding.get(k) != article.get(k) for k in ("article_id", "canonical_url", "source_id")):
        return False
    version = next((r for r in tables.get("article_versions", []) if r["version_id"] == article["current_version_id"]), {})
    if binding.get("version") != ([{k: v for k, v in version.items() if k not in {"first_seen", "last_seen"}}] if version else []):
        return False
    for key, table in (("content", "article_content_versions"), ("enrichments", "article_enrichments"),
            ("acquisition", "acquisition_items"), ("date_observations", "article_date_observations")):
        if any(not any(all(row.get(k) == v for k, v in item.items()) for row in tables.get(table, [])) for item in binding.get(key, [])):
            return False
    if article.get("current_content_version_id") and not any(r.get("content_version_id") == article["current_content_version_id"] for r in binding.get("content", [])):
        return False
    selected = (binding.get("acquisition") or [{}])[-1]
    dated = [r for r in binding.get("acquisition", []) if r.get("publication_date") and r.get("publication_date_evidence_json") and r.get("processing_status") == "complete"]
    publication = max(dated, key=lambda r: r["publication_date"]) if dated else {}
    fields = source_fields("articles", {"title": version.get("observed_title") or selected.get("title"),
        "summary": version.get("observed_summary") or selected.get("summary"),
        "publication_date": publication.get("publication_date"),
        "publication_date_evidence": json.loads(publication["publication_date_evidence_json"]) if publication else None})
    return fields == target["fields"]


def frozen_targets(snapshot):
    tables = snapshot.get("tables", {})
    targets = []
    for table, kind, identity, payload in (("pdf_intake_article_occurrences", "articles", "article_id", "occurrence_json"),
            ("pdf_intake_calendar_items", "meetings", "event_id", "item_json")):
        for row in tables.get(table, []):
            item = dict(json.loads(row[payload]), occurrence_id=row["occurrence_id"], source_document_sha256=row["source_document_sha256"])
            if kind == "articles":
                item["raw_url"] = item.get("raw_url") or row["raw_url"]
            binding = {k: row[k] for k in ("occurrence_id", identity, "source_document_sha256", "page", "content_sha256")}
            binding["source_payload_sha256"] = _sha(json.loads(row[payload]))
            if kind == "articles":
                binding["raw_url"] = row["raw_url"]
            for url in dict.fromkeys((item.get("source_urls") if kind == "meetings" else [item.get("raw_url")]) or [""]):
                targets.append({"check_kind": kind, "occurrence_id": row["occurrence_id"], "source_url": url or "",
                    "source_revision_sha256": source_revision(kind, item), "fields": source_fields(kind, item),
                    "entity_kind": "pdf_article" if kind == "articles" else "pdf_meeting", "entity_id": row[identity],
                    "source_binding": binding, "_row": row})
    for target in snapshot.get("information_targets", []):
        if target.get("entity_kind") in {"article", "meeting"}:
            targets.append({**target, "_native_binding": _native_binding(snapshot, target)})
    return targets


def _supported_fields(target, packet):
    fields = target["fields"]
    required = ({key for key in MEETING_FIELDS if key not in {"source_urls", "relevance_reason", "raw_date"}
        and fields.get(key) is not None and fields.get(key) != ""} if target["check_kind"] == "meetings"
        else {"title", "summary"} | ({"publication_date"} if fields.get("publication_date") else set()))
    return bool(required) and all((packet.get("comparisons") or {}).get(key, {}).get("status") == "supported" for key in required)


def review_context(connection):
    """Small immutable receipts, read in the same transaction as candidate evidence."""
    values = {}
    for row in connection.execute("SELECT review_id,candidate_sha256,status,receipt_json FROM registry_reviews WHERE status='pass'"):
        item = dict(row) if hasattr(row, "keys") else dict(zip(("review_id", "candidate_sha256", "status", "receipt_json"), row))
        values.setdefault(item["candidate_sha256"], []).append(item)
    from .enrichment_contract import approval_context
    for row in connection.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates WHERE json_extract(snapshot_json,'$.enrichment_scope.operation_scope') IS NOT NULL"):
        if row[0] in values:
            values[row[0]] = approval_context(connection, json.loads(row[1]), values[row[0]])
    return values


def _reviewed_summary(snapshot, observations, reviews):
    display = snapshot.get("derived_display") or {}
    if not display.get("summary") or display.get("summary_provenance") != "owner_reviewed_source_evidence":
        return None
    candidate = snapshot.get("_approved_candidate_sha256") or digest(snapshot)
    checked = []
    for observation in observations.values():
        if observation["entity_kind"] != "pdf_article" or not observation["verified"]:
            continue
        checked.extend({"attempt_id": source["attempt_id"], "run_id": source["run_id"],
            "source_url": source["source_url"], "source_revision_sha256": source["source_revision_sha256"],
            "packet_sha256": source["packet_sha256"], "body_sha256": source["reader_body_sha256"]}
            for source in observation["sources"] if source["reason"] is None)
    if not checked:
        return None
    for review in reviews:
        try:
            receipt = json.loads(review["receipt_json"])
            if (review.get("status") != "pass" or review.get("candidate_sha256") != candidate
                    or receipt.get("candidate_sha256") != candidate
                    or review.get("review_id") != digest([candidate, "pass", receipt])
                    or not receipt.get("authorization")):
                continue
            if any(value.get("verification_status", "verified") != "verified" for value in receipt.get("source_checks", [])):
                continue
            checks = [{key: value.get(key) for key in checked[0]} for value in receipt.get("source_checks", [])]
            if sorted(checks, key=digest) != sorted(checked, key=digest):
                continue
            if receipt.get("schema_version") == "owner-authorized-agent-review.v1":
                if (receipt.get("conclusion", {}).get("status") != "pass"
                        or receipt.get("display_correction") != display):
                    continue
            elif receipt.get("schema_version") == "owner-reviewed-display-summary.v1":
                browser = receipt.get("browser_evidence") or {}
                if (not receipt.get("conclusion") or browser.get("readable_without_challenge") is not True
                        or not browser.get("observed_facts") or not browser.get("page_title")
                        or browser.get("url") not in {check["source_url"] for check in checked}):
                    continue
            else:
                continue
            return {"summary": display["summary"], "summary_provenance": display["summary_provenance"]}
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return None


def _manual_public_facts(snapshot, target, row, packet, reviews):
    """The retained manual contract proves its stated scope, never the whole PDF row."""
    if target["entity_kind"] != "pdf_meeting" or packet.get("method") != "manual_official_website_review":
        return "manual_packet_invalid", None
    if row["source_revision_sha256"] != target["source_revision_sha256"] or packet.get("source_revision_sha256") != target["source_revision_sha256"]:
        return "stale_revision", None
    raw = json.loads(target["_row"]["item_json"])
    reader, reference = packet.get("reader") or {}, packet.get("evidence_reference") or {}
    identity = reader.get("identity_evidence") or {}
    url = target["source_url"]
    if (packet.get("occurrence_id") != target["occurrence_id"] or reference.get("source_url") != url
            or reader.get("method") != packet["method"] or not reader.get("evidence_access")
            or reader.get("evidence_reference") != url or identity.get("source_url") != url):
        return "manual_source_binding_invalid", None
    if not identity.get("title_excerpt") or not identity.get("date_excerpt"):
        return "manual_identity_evidence_missing", None
    source = (raw.get("website_source_evidence") or {}).get(url, {})
    if (source.get("identity_evidence") != identity or source.get("source_url") != url
            or source.get("evidence_access") != reader["evidence_access"]):
        return "manual_source_binding_invalid", None
    run = next((r for r in snapshot.get("dependencies", {}).get("meeting_check_runs", []) if r["run_id"] == row["run_id"]), None)
    frozen = json.loads(run["input_json"]) if run else {}
    targets = frozen.get("manual_targets", [])
    matched = [r for r in targets if r.get("occurrence_id") == target["occurrence_id"] and r.get("source_url") == url]
    if (not run or hashlib.sha256(run["input_json"].encode()).hexdigest() != run["input_sha256"]
            or frozen.get("schema_version") != packet["schema_version"] or frozen.get("kind") != "meetings"
            or frozen.get("run_id") != row["run_id"] or frozen.get("created_at") != run["created_at"]
            or frozen.get("checked_at") != row["checked_at"] or run["status"] != "complete" or not run.get("completed_at")
            or not targets or run["item_count"] != len(targets) or run["completed_count"] != len(targets) or len(matched) != 1):
        return "manual_run_binding_invalid", None
    bound = matched[0]
    if (bound.get("event_id") != target["entity_id"] or bound.get("source_revision_sha256") != target["source_revision_sha256"]
            or any(bound.get(k) != packet.get(k) for k in ("access_status", "reviewed_scope", "confirmed_field_evidence", "date_update_evidence"))
            or reader.get("date_update_evidence") != packet.get("date_update_evidence")
            or any(reference.get(k) != frozen.get(k) for k in ("facts_sha256", "prior_independent_review_sha256"))
            or reference.get("reviewer_id") != frozen.get("prior_reviewer_id")):
        return "manual_run_binding_invalid", None
    def sha(value):
        return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    if not all(sha(reference.get(k)) for k in ("facts_sha256", "prior_independent_review_sha256")) or not reference.get("reviewer_id"):
        return "manual_review_binding_invalid", None
    candidate_sha = digest(snapshot)
    accepted = None
    for review in reviews:
        receipt = json.loads(review["receipt_json"])
        if (review.get("status") == "pass" and review.get("candidate_sha256") == candidate_sha
                and review.get("review_id") == digest([candidate_sha, "pass", receipt])
                and receipt.get("decision") == "pass" and receipt.get("reviewer_independent") is True
                and receipt.get("candidate_sha256") == candidate_sha and receipt.get("entity_kind") == "pdf_meeting"
                and receipt.get("event_id") == target["entity_id"] and receipt.get("occurrence_id") == target["occurrence_id"]
                and receipt.get("facts_sha256") == reference["facts_sha256"] and receipt.get("reviewer_id")
                and all(sha(receipt.get(k)) for k in ("facts_sha256", "report_sha256", "plan_sha256", "stage_manifest_sha256"))):
            accepted = (review, receipt)
            break
    if accepted is None:
        return "manual_review_binding_missing", None
    if row["verification_status"] != "verified":
        return row["verification_status"], None
    scope = packet.get("reviewed_scope") or []
    if "identity:title_date" not in scope:
        return "manual_identity_evidence_missing", None
    facts = {"name": identity["title_excerpt"], "raw_date": identity["date_excerpt"], "source_urls": [url]}
    from climate_monitor.pdf_intake import _parse_calendar_date
    date_text = identity["date_excerpt"]
    month_first = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?,?\s+(\d{4})", date_text.strip())
    if month_first:
        month, start, end, year = month_first.groups()
        date_text = start + ("-"+end if end else "") + " " + month + " " + year
    if not re.search(r"\b\d{4}\b", date_text):
        years = set(re.findall(r"\b(?:19|20)\d{2}\b", identity["title_excerpt"]))
        if len(years) == 1:
            date_text += " " + next(iter(years))
    dates = _parse_calendar_date(date_text)
    facts.update(dates)
    facts["date_evidence"] = identity["date_excerpt"]
    excluded = ["manual_date_outside_evidence:"+k for k in ("start_date", "end_date", "date_precision")
        if facts.get(k) != target["fields"].get(k)]
    update = packet.get("date_update_evidence")
    if update and any(value != raw.get(k) for k, value in update.get("after", {}).items()):
        return "manual_source_binding_invalid", None
    for key, evidence in (packet.get("confirmed_field_evidence") or {}).items():
        confirmation = (raw.get("website_enrichment_evidence") or {}).get(key, {})
        if (key not in scope or not evidence.get("excerpt") or evidence.get("value") != target["fields"].get(key)
                or confirmation.get("confirmed_value") != evidence.get("value") or confirmation.get("quoted_text") != evidence["excerpt"]
                or confirmation.get("source_url") != url):
            return "manual_field_value_mismatch:"+key, None
        facts[key] = evidence["value"]
    for key in (*MEETING_FIELDS, "summary", "relevance"):
        if raw.get(key) and key not in facts:
            excluded.append("manual_field_outside_reviewed_scope:"+key)
    return None, {"public_facts": facts, "excluded_fields": sorted(set(excluded)),
        "verification_basis": packet["method"], "reviewed_scope": scope, "evidence_access": reader["evidence_access"],
        "access_status": row["access_status"],
        "review_id": accepted[0]["review_id"], "report_sha256": accepted[1]["report_sha256"],
        "candidate_sha256": candidate_sha, "packet_sha256": row["packet_sha256"], "run_sha256": run["input_sha256"],
        "audit_limitations": ["evidence_original_not_recovered"]}


def evaluate_snapshot(snapshot, *, reviews=(), packet_cache=None, candidate_binding=None):
    """Only evidence frozen in this exact version can establish its public facts."""
    from .owner_dates import OVERLAY, date_verdict
    if OVERLAY in snapshot:
        return date_verdict(snapshot, reviews)
    tables, dependencies = snapshot.get("tables", {}), snapshot.get("dependencies", {})
    results = {}
    for target in frozen_targets(snapshot):
        prefix = "article" if target["check_kind"] == "articles" else "meeting"
        identity = target["occurrence_id"]
        result = results.setdefault(identity, {"occurrence_id": identity, "entity_kind": target["entity_kind"],
            "entity_id": target["entity_id"], "fields": target["fields"], "source_binding": target["source_binding"],
            "sources": [], "checks": [], "reasons": []})
        attempts = [(index, row) for index, row in enumerate(tables.get(prefix + "_check_attempts", []))
            if attempt_identity(row, prefix) == identity and row.get("source_url") == target["source_url"]]
        row = max(attempts, key=lambda pair: (pair[1].get("checked_at") or "", pair[0]))[1] if attempts else None
        reason, packet, manual, owner = None, {}, None, None
        if target.get("_native_binding") is False:
            reason = "no_native_binding"
        elif row is None:
            reason = "no_check"
        else:
            try:
                packet = verified_packet_from_attempt(row, packet_cache=packet_cache, candidate_binding=candidate_binding)
                if any(packet.get(k) != row[k] for k in ("checked_at", "access_status", "verification_status")):
                    reason = "invalid_check_packet"
                elif packet.get("schema_version") == "manual-website-verification.v1":
                    reason, manual = _manual_public_facts(snapshot, target, row, packet, reviews)
                elif packet.get("schema_version") in {"owner-agent-information-check.v1", "owner-agent-information-check.v2"}:
                    from .owner_checks import public_owner_verification
                    reason, owner = public_owner_verification(snapshot, target, row, packet, reviews)
                elif row["source_revision_sha256"] != target["source_revision_sha256"] or packet.get("fields") != target["fields"]:
                    reason = "stale_revision"
                elif packet.get("source_revision_sha256") != row["source_revision_sha256"] or packet.get("occurrence_id") != identity or packet.get("source_url") != target["source_url"]:
                    reason = "invalid_source_binding"
                elif target["entity_kind"] in {"article", "meeting"} and packet.get("schema_version") != "information-check.v2":
                    reason = "no_native_binding"
                elif packet.get("schema_version") == "information-check.v2" and any(packet.get(k) != target[k] for k in ("entity_kind", "entity_id", "source_binding")):
                    reason = "invalid_source_binding"
                else:
                    run = next((r for r in dependencies.get(prefix + "_check_runs", []) if r["run_id"] == row["run_id"]), None)
                    frozen = json.loads(run["input_json"]) if run else {}
                    if not run or hashlib.sha256(run["input_json"].encode()).hexdigest() != run["input_sha256"] or run["status"] not in {"complete", "partial"} or not run.get("completed_at") or not run["item_count"] or run["item_count"] != len(frozen.get("targets", [])) or run["completed_count"] != run["item_count"]:
                        reason = "incomplete_t1"
                    elif not any(all(item.get(k) == target[k] for k in ("occurrence_id", "source_url", "source_revision_sha256", "fields")) for item in frozen.get("targets", [])):
                        reason = "invalid_source_binding"
                    elif packet.get("access_status") != "accessible":
                        reason = "failed_access"
                    elif packet.get("verification_status") != "verified":
                        reason = packet.get("verification_status") or "unchecked"
                    elif not _supported_fields(target, packet):
                        reason = "unsupported_fields"
                    elif prefix == "meeting" and not packet.get("website_candidate"):
                        reason = "unsupported_fields"
                    else:
                        reader = packet.get("reader") or {}
                        content = reader.get("content")
                        if reader.get("status") != "ok" or not isinstance(content, str) or not content.strip() or hashlib.sha256(content.encode()).hexdigest() != reader.get("content_hash"):
                            reason = "invalid_reader_evidence"
            except (ValueError, KeyError, TypeError):
                reason = "invalid_check_packet"
        if manual:
            result["manual_verification"] = manual
        if owner:
            result["owner_agent_verification"] = owner
            result.setdefault("original_fields", result["fields"])
            result["fields"] = owner["public_fields"]
        result["sources"].append({"source_url": target["source_url"], "source_revision_sha256": target["source_revision_sha256"],
            "checked_at": row.get("checked_at") if row else None, "attempt_id": row.get("attempt_id") if row else None, "run_id": row.get("run_id") if row else None,
            "access_status": row.get("access_status") if row else "unchecked", "verification_status": row.get("verification_status") if row else "unchecked",
            "reader_body_sha256": ((owner or {}).get("capture_sha256") or (packet.get("reader") or {}).get("content_hash")) if reason is None else None,
            "packet_sha256": row.get("packet_sha256") if row else None,
            "reason": reason, "error": packet.get("error") or (packet.get("reader") or {}).get("failure_reason"),
            "comparisons": packet.get("comparisons") or {}})
        effective = dict(packet, source_url=target["source_url"], website_candidate=packet.get("website_candidate"))
        if owner:
            effective["website_candidate"] = owner["public_fields"] if prefix == "meeting" else None
        effective.setdefault("checked_at", "")
        effective.setdefault("access_status", "unchecked")
        effective.setdefault("verification_status", "unchecked")
        if reason:
            effective.update(verification_status="conflict" if reason == "conflict" else "partial" if reason == "partial" else "unchecked", access_status="unchecked")
            result["reasons"].append(reason)
        result["checks"].append(effective)
    for item in results.values():
        state = _check_state(item.pop("checks"))
        item.update(verification_status=state["verification_status"], access_status=state["access_status"], collection_status=state["collection_status"], collected_candidate=state["collected_candidate"])
        item["verified"] = state["verification_status"] == "verified"
    native = [r for r in results.values() if r["entity_kind"] in {"article", "meeting"} and r["verified"]]
    verified = [r for r in results.values() if r["verified"]]
    primary = (native or verified or [None])[0]
    from .enrichment_contract import reviewed_content_receipt, inherited_content_scope
    content = reviewed_content_receipt(snapshot, reviews)
    missing_content = inherited_content_scope(snapshot) and not content
    return {"eligible": bool(primary) and not missing_content, "primary": primary, "observations": results,
        "reviewed_content": content,
        "reviewed_summary": _reviewed_summary(snapshot, results, reviews),
        "reasons": (["inherited_content_approval_missing"] if missing_content else []) + (sorted({reason for r in results.values() for reason in r["reasons"]}) or ([] if primary else ["no_check"]))}


ASSOCIATION_FIELDS = {"occurrence_id", "source_document_sha256", "source_filename", "source_document", "report_date",
    "page", "raw_url", "event_id", "article_id", "source_urls", "imported_at", "summary_basis"}


def _primary_display(snapshot, verdict):
    primary = verdict.get("primary")
    if not primary:
        return None
    fields = (primary.get("manual_verification") or {}).get("public_facts") or primary.get("collected_candidate") or primary["fields"]
    url = next((source["source_url"] for source in primary["sources"] if source["reason"] is None), None)
    article = next(iter(snapshot.get("tables", {}).get("articles", [])), {})
    return {"title": fields.get("title") or fields.get("anchor_text"), "name": fields.get("name"),
        "source_url": article.get("canonical_url") or url}


def _owner_article_date(owner, article):
    """A derived date observation cites the actual exact-candidate owner receipt."""
    fields = owner["public_fields"]
    if not fields.get("publication_date"):
        return []
    evidence = {"source_system": "owner_agent", "table": "registry_reviews", "record_id": owner["review_id"],
        "source_url": owner["source_url"], "match_basis": "independently_reviewed_source_excerpt",
        "date_kind": "publication_date", **owner.get("publication_date_evidence", {})}
    return [{"observation_id": "owner-date-"+owner["review_id"], "article_id": article["article_id"],
        "canonical_url": article["canonical_url"], "observation_kind": "page_information",
        "observed_at": fields["publication_date"], "source_system": "owner_agent", "source_database": "frozen_owner_check",
        "source_table": "registry_reviews", "source_record_id": owner["review_id"], "evidence_json": json.dumps(evidence),
        "recorded_at": owner["checked_at"]}]


def _public_reviewed_date_rows(snapshot, table, verdict):
    """Keep only independently reviewed typed dates bound to this exact page."""
    if not verdict.get("eligible"):
        return []
    from climate_monitor.dedupe import canonical_url
    from .article_dates import ArticleDateImportError, _article_event_from_observation, _typed_date_kind, _typed_information_date

    tables = snapshot.get("tables", {})
    if snapshot.get("entity_kind") == "article":
        owner = next(iter(tables.get("articles", [])), {})
        owner_ids = {owner.get("article_id")} if owner.get("article_id") else set()
        owner_url = owner.get("canonical_url")
        if table == "pdf_article_date_observations":
            owner_ids = {row.get("article_id") for row in tables.get("pdf_intake_articles", [])
                if row.get("canonical_url") == owner_url}
    elif snapshot.get("entity_kind") == "pdf_article":
        owner = next(iter(tables.get("pdf_intake_articles", [])), {})
        owner_ids = {owner.get("article_id")} if owner.get("article_id") else set()
        owner_url = owner.get("canonical_url")
        if table == "article_date_observations":
            owner_ids = set()
    else:
        return []
    if not owner_url:
        return []
    try:
        canonical_owner_url = canonical_url(owner_url)
    except (TypeError, UnicodeError, ValueError):
        return []

    approved = []
    for row in tables.get(table, []):
        if row.get("observation_kind") != "page_information" or row.get("article_id") not in owner_ids:
            continue
        try:
            evidence = json.loads(row.get("evidence_json") or "null")
            if (not isinstance(evidence, dict) or evidence.get("review_status") != "pass"
                    or not all(isinstance(evidence.get(key), str) and evidence[key].strip()
                        for key in ("source_url", "target_url", "field", "text"))):
                continue
            if canonical_url(row.get("canonical_url") or "") != canonical_owner_url:
                continue
            if canonical_url(evidence["target_url"]) != canonical_owner_url:
                continue
            date_kind = _typed_date_kind(evidence)
            if date_kind is None:
                continue
            _typed_information_date(row.get("observed_at"), evidence.get("date_precision"))
            if date_kind == "event":
                _article_event_from_observation({"kind": "page_information", **row,
                    "observed_at": row.get("observed_at"), "evidence": evidence})
        except (ArticleDateImportError, json.JSONDecodeError, TypeError, UnicodeError, ValueError):
            continue
        approved.append(row)
    return approved


def public_snapshot_view(snapshot, verdict, canonical=None, primary_display=None):
    """A derived read projection; original candidate JSON and SHA never change."""
    from .owner_dates import OVERLAY, date_public_view
    if OVERLAY in snapshot:
        return date_public_view(snapshot, verdict)
    view = deepcopy(snapshot)
    view["_approved_candidate_sha256"] = snapshot.get("_approved_candidate_sha256") or digest(snapshot)
    reference = {"entity_kind": snapshot["entity_kind"], "entity_id": snapshot["entity_id"],
        "candidate_sha256": view["_approved_candidate_sha256"]}
    if canonical:
        reference = canonical
    primary_display = primary_display or _primary_display(snapshot, verdict)
    verified = {key for key, result in verdict["observations"].items() if result["verified"]}
    tables = view["tables"]
    removed_date_observations = False
    reviewed_date_rows = {}
    for table in ("article_date_observations", "pdf_article_date_observations"):
        if table not in tables:
            continue
        rows = tables.get(table, [])
        reviewed = _public_reviewed_date_rows(view, table, verdict)
        reviewed_date_rows[table] = reviewed
        if len(reviewed) != len(rows):
            removed_date_observations = True
        tables[table] = reviewed
    hidden = view["_hidden_factual_fields"] = {}
    if (verdict["primary"] and snapshot.get("derived_display", {}).get("summary_provenance") == "owner_reviewed_source_evidence"
            and not verdict.get("reviewed_summary")):
        hidden[verdict["primary"]["occurrence_id"]] = ["reviewed_summary_receipt_missing"]
    view["_manual_public_verifications"] = {}
    for table, payload in (("pdf_intake_article_occurrences", "occurrence_json"), ("pdf_intake_calendar_items", "item_json")):
        for row in tables.get(table, []):
            observation = verdict["observations"].get(row["occurrence_id"], {})
            manual = observation.get("manual_verification") if observation.get("verified") else None
            owner = observation.get("owner_agent_verification") if observation.get("verified") else None
            if owner:
                raw = json.loads(row[payload])
                public = {key: value for key, value in raw.items() if key in ASSOCIATION_FIELDS}
                # Source URLs are associations only when they are the checked URL.
                public.pop("source_urls", None)
                public.update(owner["public_fields"], owner_agent_verification=owner)
                if table == "pdf_intake_calendar_items":
                    checked_url = owner.get("final_url") or owner["source_url"]
                    public["checked_source_url"] = checked_url
                    if checked_url != owner["source_url"]:
                        public["original_source_url"] = owner["source_url"]
                    public = {**{key: None for key in MEETING_FIELDS}, **public, "source_urls": [checked_url]}
                    public["summary"] = ""
                    row.update(name=public["name"], raw_date=public.get("raw_date") or "", start_date=public.get("start_date"),
                        end_date=public.get("end_date"), date_precision=public["date_precision"], summary="", type_safe_classification_json=None)
                else:
                    public["checked_source_url"] = owner.get("final_url") or owner["source_url"]
                    public["summary_basis"] = "owner_agent_source_excerpt"
                    public["report_fields"] = None
                    row["publication_date"] = public.get("publication_date")
                row[payload] = json.dumps(public, ensure_ascii=False)
                hidden[row["occurrence_id"]] = ["owner_field_excluded:"+field for field in owner["excluded_fields"]]
                continue
            if manual:
                raw = json.loads(row[payload])
                facts = manual["public_facts"]
                public = {key: value for key, value in raw.items() if key in ASSOCIATION_FIELDS}
                public.update({key: None for key in MEETING_FIELDS})
                public.update(facts, summary="", kind=None, manual_verification=manual)
                row[payload] = json.dumps(public, ensure_ascii=False)
                row.update(name=facts["name"], raw_date=facts["raw_date"], start_date=facts.get("start_date"),
                    end_date=facts.get("end_date"), date_precision=facts.get("date_precision") or "unknown", summary="", type_safe_classification_json=None)
                view["_manual_public_verifications"][row["occurrence_id"]] = {**manual,
                    "checked_at": max((source.get("checked_at") or "" for source in observation.get("sources", [])), default=""),
                    "access_status": observation["access_status"], "verification_status": "verified", "collection_status": "collected",
                    "checks": [], "collected_candidate": None, "canonical_event_id": None}
                continue
            if row["occurrence_id"] in verified:
                if table == "pdf_intake_calendar_items":
                    raw = json.loads(row[payload])
                    public = {key: value for key, value in raw.items()
                        if key in ASSOCIATION_FIELDS or key in observation["fields"]}
                    public.update(observation["fields"])
                    # T1 checks structured meeting fields, not the PDF's descriptive prose.
                    public["summary"] = ""
                    hidden[row["occurrence_id"]] = ["field_outside_verification:"+key
                        for key in ("summary", "raw_text", "description", "table_description") if raw.get(key)]
                    row[payload] = json.dumps(public, ensure_ascii=False)
                    row["summary"] = ""
                else:
                    from climate_monitor.pdf_intake import report_update_fields
                    raw = json.loads(row[payload])
                    document = next((value for value in view.get("dependencies", {}).get("pdf_intake_documents", [])
                        if value["document_sha256"] == row["source_document_sha256"]), {})
                    fields = (report_update_fields(dict(raw, page=row["page"], raw_url=row["raw_url"]),
                        json.loads(document.get("document_json") or "{}"))
                        if re.search(r"\nIN WINDOW[^\n]+\n", raw.get("summary") or "", re.I) else None)
                    if fields and fields.get("publisher"):
                        hidden.setdefault(row["occurrence_id"], []).append("field_outside_verification:report_fields.publisher")
                        fields["publisher"] = None
                    raw["report_fields"] = fields
                    row[payload] = json.dumps(raw, ensure_ascii=False)
                continue
            raw = json.loads(row[payload])
            metadata = {key: value for key, value in raw.items() if key in ASSOCIATION_FIELDS}
            metadata.update({key: row[key] for key in ("occurrence_id", "source_document_sha256", "page")})
            metadata.update(metadata_only=True, verified_primary_reference=reference,
                association_candidate_sha256=view["_approved_candidate_sha256"])
            if primary_display:
                metadata["verified_primary_display"] = primary_display
            row[payload] = json.dumps(metadata, ensure_ascii=False)
            if table == "pdf_intake_calendar_items":
                row.update(name="", raw_date="", date_precision="unknown", start_date=None, end_date=None, summary="", type_safe_classification_json=None)
            else:
                row.update(publication_date=None, type_safe_classification_json=None) if "type_safe_classification_json" in row else row.update(publication_date=None)
    verified_pdf_articles = {row["article_id"] for row in tables.get("pdf_intake_article_occurrences", []) if row["occurrence_id"] in verified}
    for row in tables.get("pdf_intake_articles", []):
        if row["article_id"] not in verified_pdf_articles:
            row.update(title=(primary_display or {}).get("title") or "Verified article reference", type_safe_classification_json=None)
        else:
            owner = next((value.get("owner_agent_verification") for value in verdict["observations"].values()
                if value["entity_kind"] == "pdf_article" and value["entity_id"] == row["article_id"] and value["verified"] and value.get("owner_agent_verification")), None)
            if owner:
                row["title"] = owner["public_fields"]["title"]
                row["type_safe_classification_json"] = None
    checked_content = {row.get("content_version_id") for row in tables.get("climate_event_sources", []) if row.get("event_source_id") in verified}
    unchecked_content = set()
    for row in tables.get("climate_event_sources", []):
        if row.get("event_source_id") not in verified:
            unchecked_content.add(row.get("content_version_id"))
            row["candidate_json"] = "{}"
            for field in ("date_evidence", "deadline_evidence", "status_evidence"):
                if field in row:
                    row[field] = None
    for row in view.get("dependencies", {}).get("article_content_versions", []):
        if row.get("content_version_id") in unchecked_content - checked_content:
            row["markdown_content"] = ""
    # Historical observations retain citation metadata; only the checked primary supplies facts.
    current = next(iter(tables.get("articles", [])), {}).get("current_version_id")
    for row in tables.get("article_versions", []):
        if row.get("version_id") != current:
            row.update(observed_summary="", observed_title="", canonical_title="")
    for table in ("acquisition_items", "discoveries"):
        for row in tables.get(table, []):
            for field in ("summary", "title", "observed_summary", "observed_title"):
                if field in row:
                    row[field] = ""
    view.pop("static_payload", None)
    view.pop("git_wiki", None)
    view["report_fallbacks"] = {}
    view["fallback_annotations"] = {}
    if not verdict["eligible"]:
        view["derived_display"] = {key: value for key, value in view.get("derived_display", {}).items() if key == "canonical_pdf_event_id"}
        view["supplements"] = []
        for row in tables.get("pdf_intake_articles", []):
            row.update(title=(primary_display or {}).get("title") or "Verified article reference", type_safe_classification_json=None)
    elif snapshot["entity_kind"] == "pdf_article":
        fields = verdict["primary"]["fields"]
        from .publication import approved_display
        classification = {key: value for key, value in approved_display(snapshot).items()
            if key in {"categories", "keywords", "metadata_provenance", "supplement_generator"}}
        owner = verdict["primary"].get("owner_agent_verification")
        view["derived_display"] = {**classification, "summary": fields.get("summary") or "",
            "summary_provenance": "owner_agent_verified_source" if owner else "verified_pdf_source"}
        if owner:
            view["derived_display"]["owner_agent_verification"] = owner
        view["supplements"] = []
        for row in tables.get("pdf_intake_articles", []):
            row["title"] = fields.get("title") or fields.get("anchor_text") or "Verified PDF article"
    view["_verification"] = {"verified_occurrence_ids": sorted(verified), "primary": verdict["primary"], "metadata_only": not verdict["eligible"]}
    native_primary = next((item for item in verdict["observations"].values() if item["verified"] and item["entity_kind"] == "article"), None)
    if snapshot["entity_kind"] == "article" and native_primary:
        # A source binding does not verify a separately generated enrichment summary.
        view["fallback_annotations"] = {url: {**annotation, "title": "", "summary": ""}
            for url, annotation in snapshot.get("fallback_annotations", {}).items()}
        from .publication import approved_display
        classification = {key: value for key, value in approved_display(snapshot).items()
            if key in {"categories", "keywords", "metadata_provenance", "supplement_generator"}}
        article = next(iter(tables.get("articles", [])), {})
        reports = {row["report_id"]: row for row in view.get("dependencies", {}).get("reports", [])}
        discoveries = {row["discovery_id"]: row for row in tables.get("discoveries", [])}
        fallback_classification = {}
        fields = native_primary["fields"]
        if native_primary.get("owner_agent_verification"):
            for row in tables.get("article_versions", []):
                if row.get("version_id") == current:
                    row.update(observed_title=fields["title"], canonical_title=fields["title"], observed_summary=fields["summary"])
            view.setdefault("derived_display", {})["owner_agent_verification"] = native_primary["owner_agent_verification"]
        hidden_report_fields = set()
        from climate_monitor.dedupe import canonical_url
        for appearance in sorted(tables.get("report_appearances", []), key=lambda row:
                (row.get("ordinal") or 0, reports.get(row["report_id"], {}).get("filename") or "")):
            report = reports.get(appearance["report_id"], {})
            fallback = snapshot.get("report_fallbacks", {}).get(report.get("report_date"))
            ordinal = (appearance.get("ordinal") or 0) - 1
            discovery = discoveries.get(appearance.get("discovery_id"), {})
            if (appearance.get("article_id") != article.get("article_id") or not fallback
                    or fallback.get("sha256") != report.get("report_sha256") or fallback.get("path") != report.get("filename")
                    or fallback.get("report_date") != report.get("report_date") or not 0 <= ordinal < len(fallback.get("articles", []))):
                continue
            source = fallback["articles"][ordinal]
            try:
                matches = canonical_url(source.get("url") or "") == canonical_url(discovery.get("raw_url") or "")
            except (TypeError, UnicodeError, ValueError):
                matches = False
            if not matches:
                continue
            hidden_report_fields.update("field_outside_verification:report_fallbacks."+key for key in ("title", "summary")
                if source.get(key) and source[key] != fields.get(key))
            if report["report_date"] not in view["report_fallbacks"]:
                view["report_fallbacks"][report["report_date"]] = {**fallback, "executive_summary": [], "warnings": [],
                    "articles": [{**item, "title": "", "summary": "", "url": "", "categories": [], "keywords": []}
                        for item in fallback["articles"]]}
            view["report_fallbacks"][report["report_date"]]["articles"][ordinal] = {**source, "title": "", "summary": ""}
            if not fallback_classification and source.get("summary", "").strip() and source.get("categories") and source.get("keywords"):
                fallback_classification = {"categories": source["categories"], "keywords": source["keywords"],
                    "metadata_provenance": {"categories": "source_report", "keywords": "source_report"}}
        has_enrichment = any(row.get("status") == "complete" and (
            article.get("current_content_version_id") is not None and row.get("content_version_id") == article["current_content_version_id"]
            or row.get("content_version_id") is None and row.get("article_version_id") == article.get("current_version_id"))
            for row in tables.get("article_enrichments", []))
        if not has_enrichment and not view["fallback_annotations"].get(article.get("canonical_url")):
            classification = {**fallback_classification, **classification}
        fields = native_primary["fields"]
        excluded = hidden.setdefault(native_primary["occurrence_id"], [])
        excluded.extend(sorted(hidden_report_fields))
        for row in tables.get("article_enrichments", []):
            if row.get("summary") and row["summary"] != fields.get("summary"):
                excluded.append("field_outside_verification:enrichment.summary")
            if row.get("status") == "complete":
                row["summary"] = fields.get("summary")
        for row in tables.get("article_semantics", []):
            excluded.extend("field_outside_verification:semantics."+key for key in ("title", "summary") if row.get(key))
            row.update(title=None, summary=None)
        checked_bodies = {source["reader_body_sha256"] for source in native_primary["sources"] if source.get("reason") is None}
        for row in tables.get("article_content_versions", []):
            if hashlib.sha256(row["markdown_content"].encode()).hexdigest() not in checked_bodies:
                if row["markdown_content"]:
                    excluded.append("reader_body_mismatch")
                row["markdown_content"] = ""
        if removed_date_observations:
            excluded.append("unverified_date_observation")
        owner = native_primary.get("owner_agent_verification")
        if owner:
            tables["article_date_observations"] = (
                _owner_article_date(owner, article)
                + reviewed_date_rows.get("article_date_observations", [])
            )
        for row in tables.get("acquisition_items", []):
            if (row.get("publication_date") != fields.get("publication_date")
                    or json.loads(row.get("publication_date_evidence_json") or "null") != fields.get("publication_date_evidence")):
                if row.get("publication_date") or row.get("publication_date_evidence_json"):
                    excluded.append("unverified_acquisition_date")
                row.update(publication_date=None, publication_date_evidence_json=None, date_status="unknown_pending_review")
        if any(row.get("summary") for row in view.get("supplements", [])):
            excluded.append("field_outside_verification:supplements.summary")
        if view.get("derived_display", {}).get("summary") and view["derived_display"]["summary"] != fields.get("summary") and not verdict.get("reviewed_summary"):
            excluded.append("field_outside_verification:derived_display.summary")
        view["supplements"] = []
        view["derived_display"] = {**view.get("derived_display", {}), **classification, "title": fields.get("title"), "summary": fields.get("summary") or "",
            "summary_provenance": "owner_agent_verified_source" if native_primary.get("owner_agent_verification") else "verified_source_evidence"}
    if snapshot["entity_kind"] == "meeting" and verdict["primary"] and verdict["primary"].get("owner_agent_verification"):
        fields = verdict["primary"]["fields"]
        owner = verdict["primary"]["owner_agent_verification"]
        display = view.setdefault("derived_display", {})
        display["owner_agent_verification"] = owner
        display["checked_source_url"] = owner.get("final_url") or owner["source_url"]
        if owner.get("source_url") != display["checked_source_url"]:
            display["original_source_url"] = owner["source_url"]
        for row in tables.get("climate_events", []):
            for key in MEETING_FIELDS:
                if key in row:
                    row[key] = fields.get(key)
        for row in tables.get("climate_event_sources", []):
            row["candidate_json"] = json.dumps({**fields, "_single_day_supported": fields.get("date_precision") == "day" and bool(fields.get("start_date")) and not fields.get("end_date")})
            for field in ("date_evidence", "deadline_evidence", "status_evidence"):
                if field in row:
                    row[field] = None
    if snapshot["entity_kind"] in {"article", "meeting"} and not any(r["verified"] and r["entity_kind"] == snapshot["entity_kind"] for r in verdict["observations"].values()):
        # A checked PDF source can be the primary of an already-linked canonical article.
        if snapshot["entity_kind"] == "article" and verdict["primary"]:
            fields = verdict["primary"]["fields"]
            for row in tables.get("article_versions", []):
                if row.get("version_id") == current:
                    row.update(observed_title=fields.get("title") or fields.get("anchor_text") or "Verified PDF article", observed_summary=fields.get("summary") or "")
            for table in ("article_content_versions", "article_enrichments", "article_semantics"):
                tables[table] = []
            tables["article_date_observations"] = reviewed_date_rows.get("article_date_observations", [])
            for row in tables.get("articles", []):
                row["current_content_version_id"] = None
            for row in tables.get("acquisition_items", []):
                row.update(publication_date=None, publication_date_evidence_json=None, date_status="unknown_pending_review")
            view["supplements"] = []
            owner = verdict["primary"].get("owner_agent_verification")
            view["derived_display"] = {"summary": fields.get("summary") or "",
                "summary_provenance": "owner_agent_verified_source" if owner else "verified_pdf_source"}
            if owner:
                view["derived_display"]["owner_agent_verification"] = owner
                tables["article_date_observations"] = (
                    _owner_article_date(owner, tables["articles"][0])
                    + reviewed_date_rows.get("article_date_observations", [])
                )
    owner = (verdict.get("primary") or {}).get("owner_agent_verification")
    if owner:
        # Owner review covers only public_fields, never retained classifier output
        # or a generated enrichment/annotation bundle from the original record.
        for table in ("article_enrichments", "article_semantics"):
            if table in tables:
                tables[table] = []
        for row in tables.get("pdf_intake_articles", []):
            row["type_safe_classification_json"] = None
        view["fallback_annotations"], view["report_fallbacks"], view["supplements"] = {}, {}, []
        display = view.get("derived_display", {})
        view["derived_display"] = {key: value for key, value in display.items() if key in {
            "title", "summary", "summary_provenance", "canonical_pdf_event_id", "owner_agent_verification",
            "checked_source_url", "original_source_url"}}
        if snapshot["entity_kind"] in {"article", "pdf_article"}:
            view["derived_display"].update(categories=[], keywords=[], metadata_provenance={})
            metadata = owner.get("editorial_metadata") if owner.get("verification_basis") == "owner-agent-information-check.v2" else None
            if isinstance(metadata, dict):
                view["derived_display"].update(categories=list(metadata["categories"]), keywords=list(metadata["keywords"]),
                    metadata_provenance={"categories": "owner_reviewed_editorial_classification",
                        "keywords": "owner_reviewed_editorial_classification"})
                view["derived_display"]["editorial_metadata_provenance"] = {
                    "taxonomy_id": owner["taxonomy_id"], "taxonomy_sha256": owner["taxonomy_sha256"],
                    "editorial_metadata_sha256": owner["editorial_metadata_sha256"],
                    "capture_sha256": owner["capture_sha256"],
                    "public_fields_sha256": owner["public_fields_sha256"],
                    "review_id": owner["review_id"]}
            elif verdict.get("reviewed_content"):
                from .enrichment_contract import inherited_editorial_projection
                view["derived_display"].update(inherited_editorial_projection(snapshot))
            view["derived_display"]["checked_source_url"] = owner.get("final_url") or owner["source_url"]
            if owner["public_fields"].get("publication_date"):
                view["derived_display"].update(publication_date=owner["public_fields"]["publication_date"],
                    publication_date_evidence=owner.get("publication_date_evidence"))
    if verdict.get("reviewed_summary"):
        view["derived_display"].update(verdict["reviewed_summary"])
    from .article_dates import validate_content_update
    bundle = validate_content_update(snapshot)
    if bundle:
        # The exact approved bundle carries its covering summary and retained comparison together.
        view["derived_display"].update(summary=bundle["summary"], content_update=deepcopy(bundle))
        view["tables"]["article_content_versions"] = deepcopy(snapshot["tables"]["article_content_versions"])
        for field, proof in bundle["date_evidence"].items():
            view["derived_display"].update({field: proof["value"], field + "_evidence": proof})
    elif (verdict.get("reviewed_content") or {}).get("initial_summary"):
        # The full G3 receipt covers this current-body enrichment, not the older source-target summary.
        initial = verdict["reviewed_content"]["initial_summary"]
        view["derived_display"].update(summary=initial["summary"], summary_provenance="exact_candidate_content_review")
        for row in view["tables"].get("article_enrichments", []):
            if row.get("content_version_id") == initial["content_version_id"] and row.get("status") == "complete":
                row["summary"] = initial["summary"]
        view["tables"]["article_content_versions"] = deepcopy(snapshot["tables"]["article_content_versions"])
    category_review = (verdict.get("reviewed_content") or {}).get("category_correction")
    if category_review:
        view["derived_display"]["categories"] = list(snapshot["derived_display"]["categories"])
        view["derived_display"].setdefault("metadata_provenance", {})["categories"] = "reviewed_category_correction"
        view["derived_display"]["category_correction_provenance"] = category_review
    from .owner_dates import carry_manual_dates, inherited_article_date
    for field in ("publication_date", "updated_date", "revision_date"):
        if snapshot.get("derived_display", {}).get(field):
            inherited = inherited_article_date(snapshot, field=field, incoming=view)
            if inherited:
                for key in (field, field + "_evidence", field + "_verification"):
                    view["derived_display"].pop(key, None)
                view["derived_display"].update(inherited)
    carry_manual_dates(view, snapshot.get("derived_display", {}).get("manual_date_overrides", {}))
    if snapshot.get("enrichment_scope"):
        from .enrichment_contract import projected_date_proofs
        view["derived_display"] = projected_date_proofs(view["derived_display"])
    return view


def canonical_target(snapshot):
    if snapshot["entity_kind"] == "pdf_meeting":
        target = snapshot.get("derived_display", {}).get("canonical_pdf_event_id")
        return ("pdf_meeting", target) if target else None
    if snapshot["entity_kind"] == "pdf_article":
        row = next(iter(snapshot.get("tables", {}).get("pdf_intake_articles", [])), {})
        return ("article", row["core_article_id"]) if row.get("core_article_id") else None
    return None


def permitted_view(snapshot, verdict, target_snapshot=None, target_verdict=None):
    if verdict["eligible"]:
        return public_snapshot_view(snapshot, verdict)
    if target_snapshot is None or not target_verdict or not target_verdict["eligible"]:
        return None
    reference = {"entity_kind": target_snapshot["entity_kind"], "entity_id": target_snapshot["entity_id"],
        "candidate_sha256": target_snapshot.get("_approved_candidate_sha256") or digest(target_snapshot)}
    return public_snapshot_view(snapshot, verdict, reference, _primary_display(target_snapshot, target_verdict))
