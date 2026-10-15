"""Private, deterministic T10 inventory; generating it never approves or publishes."""
from __future__ import annotations

import json
import hashlib
from collections import Counter
from pathlib import Path

from climate_delivery.io import atomic_write_bytes, atomic_write_json
from .acquisition_review import digest, now_stamp
from .verification import PUBLICATION_POLICY, evaluate_snapshot, canonical_target, review_context, public_snapshot_view

REASONS = {
    "reviewed_summary_receipt_missing": "额外展示摘要缺少精确来源核验及审核凭据，已回退至核验原文摘要",
    "no_check": "没有核验记录", "stale_revision": "核验绑定的原文版本已不匹配", "failed_access": "源网页读取失败",
    "conflict": "核验发现事实冲突", "partial": "部分字段未获支持", "unchecked": "尚未有效核验",
    "invalid_check_packet": "核验包或哈希无效", "invalid_source_binding": "核验缺少准确来源绑定",
    "no_native_binding": "缺少原生文章或会议的真实来源绑定", "incomplete_t1": "T1 未完整结束",
    "invalid_reader_evidence": "缺少真实完整网页内容或内容哈希不符", "unsupported_fields": "事实字段缺少来源支持",
    "no_publication": "没有已批准公开版本", "pending_review": "新候选待审核", "owner_hidden": "所有者已隐藏",
    "manual_source_access_unavailable": "原网页无法直接读取；仅公开独立人工核验支持的范围",
    "manual_packet_invalid": "人工核验格式无效", "manual_source_binding_invalid": "人工核验来源绑定不匹配",
    "manual_run_binding_invalid": "人工核验运行记录不匹配", "manual_review_binding_missing": "缺少精确候选的独立人工审查凭据",
    "manual_review_binding_invalid": "人工审查凭据绑定无效", "manual_identity_evidence_missing": "缺少人工核验身份日期证据",
    "unverified_date_observation": "该项核验字段可公开，未获核验的额外页面日期已隐藏",
    "unverified_acquisition_date": "该项核验字段可公开，未获核验的额外采集条目日期已隐藏",
    "reader_body_mismatch": "该项核验字段可公开，保留正文与核验网页内容不同，正文已隐藏",
    "missing_ineligible_target": "关联目标不存在或没有有效核验", "metadata_only": "仅保留已批准的报告页码关联，原文事实不公开"}


def reason_explanation(code):
    kind, _, field = code.partition(":")
    if kind == "field_outside_verification":
        return "该项核验字段可公开，额外字段未获核验，已隐藏："+field
    if kind == "manual_field_outside_reviewed_scope":
        return "字段未在人工核验范围内，已隐藏："+field
    if kind == "manual_date_outside_evidence":
        return "原日期超出人工证据，仅显示可证日期："+field
    if kind == "manual_field_value_mismatch":
        return "人工字段证据值不匹配："+field
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
    report = {"schema_version": "t10-verification-report.v1", "generated_at": now_stamp(), "database": str(database),
        "publication_policy": PUBLICATION_POLICY, "public_revision": revision, "totals": totals, "rows": rows,
        "source_outcomes": outcomes, "retained_verified_public_versions": retained,
        "original_summary_adoptions": [{**row, "label": "采用原文", "fact_verification": "not_asserted"} for row in adoptions]}
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
    lines = ["# T10 核验与公开清单", "", "本报告只列实际状态，不代替人工审核，也不产生 PASS。原文采用不代表独立事实核验。", "",
        "版本：current 为库内当前记录，latest 为最新候选，published 为已批准版本。同一对象可有多行；待审核的新候选可以与旧的已核验公开版本并存。", "",
        "历史人工核验仅按保留的精确独立审核凭据及范围使用；本次未重读其外部原件。", "",
        "生成：" + report["generated_at"], "数据库：" + str(database), "政策：" + PUBLICATION_POLICY, "", "## 逐项清单（无截断）", "",
        "|版本|类型 / 身份|标题|来源 / 报告页|核验|实际公开角色|原因 / 审核|", "|---|---|---|---|---|---|---|"]
    for row in rows:
        sources = "; ".join(s["source_url"] + ("; " + str(s.get("error")) if s.get("error") else "") for s in row["sources"])
        source = sources + ("; " + str((row["source_report"] or {}).get("filename") or "PDF") + " " + str(row["document_sha256"]) + " page " + str(row["page"]) if row["document_sha256"] else "")
        disposition = "; ".join(r["status"] + ": " + json.dumps(r["disposition"], ensure_ascii=False) for r in row["reviews"])
        lines.append("|" + "|".join(cell(x) for x in (row["version"],row["entity_kind"]+" / "+row["entity_id"], row["title"], source,
            row["verification_status"]+" / "+row["access_status"], row["public_role"], "; ".join(reason_explanation(reason)+" ("+reason+")" for reason in row["reason_codes"])+"; "+disposition)) + "|")
    lines.extend(["", "## 读取失败及零候选来源", "", *["- " + json.dumps(row, ensure_ascii=False) for row in outcomes],
        "", "## 保留的旧核验公开版本", "", *["- " + json.dumps(row, ensure_ascii=False) for row in retained],
        "", "## 原文采用（不代表事实核验）", "", *["- " + json.dumps(row, ensure_ascii=False) for row in report["original_summary_adoptions"]]])
    human_bytes = ("\n".join(lines)+"\n").encode()
    atomic_write_bytes(markdown_path, human_bytes)
    receipt = {"status": "written", "json_path": str(json_path), "human_report_path": str(markdown_path), "report_sha256": sha,
        "totals": totals, "row_count": len(rows), "source_outcome_count": len(outcomes),
        "semantic_sha256": semantic_sha, "human_report_sha256": hashlib.sha256(human_bytes).hexdigest(), "reused": False}
    atomic_write_json(output.parent / "latest.json", receipt)
    return receipt
