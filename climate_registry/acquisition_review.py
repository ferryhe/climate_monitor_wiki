"""Website rotation, material versions and independent acquisition review receipts.

These are external business files, not another acquisition driver. Recovery uses
ManagementService.resume and its original binding and cumulative budget.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from climate_delivery.io import atomic_write_json, transaction_lock as exclusive_lock


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("an aware UTC timestamp is required")
    return parsed.astimezone(timezone.utc)


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def knowledge_fields(fields):
    from .capture import _plain_markdown, article_body_markdown
    fields = dict(fields)
    for key in ("content", "title", "summary", "anchor_text"):
        if isinstance(fields.get(key), str):
            value = article_body_markdown(fields[key]) if key == "content" else fields[key]
            fields[key] = _plain_markdown(value or fields[key])
    def normalize(value):
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value
    return normalize(fields)


def record_knowledge(connection, *, kind, entity_id, source_kind, source_ref,
    fields, evidence, recorded_at=None, first_ingested_at=None, time_basis="transaction"):
    """Same business fields retain their original time, even across retries/checks."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_versions'").fetchone():
        return None  # Historical writer contracts stay readable.
    at = recorded_at or now_stamp()
    timestamp(at)
    fields = knowledge_fields(fields)
    material = digest(fields)
    previous = connection.execute("""SELECT material_sha256,first_ingested_at,substantive_updated_at,source_ref,knowledge_id
        FROM knowledge_versions WHERE entity_kind=? AND entity_id=? ORDER BY rowid DESC LIMIT 1""",
        (kind, entity_id)).fetchone()
    first = previous[1] if previous else first_ingested_at or (at if time_basis == "transaction" else None)
    changed = at if previous and previous[0] != material else previous[2] if previous else None
    if previous and previous[0] == material and previous[3] == source_ref:
        return previous[4]
    key = digest([kind, entity_id, source_ref, material, changed or first])
    connection.execute("""INSERT OR IGNORE INTO knowledge_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, kind, entity_id, source_kind, source_ref, material,
         json.dumps(fields, sort_keys=True, ensure_ascii=False), json.dumps(evidence, sort_keys=True),
         first, changed, at, time_basis))
    return key


def reserve_rotation(root: Path, occurrence: str, keys: list[str], *, count=5):
    if not keys or len(keys) != len(set(keys)) or count < 1:
        raise ValueError("rotation requires unique configured sources")
    root = Path(root)
    with exclusive_lock(root, "rotation"):
        path = root / "rotation.json"
        state = json.loads(path.read_text()) if path.exists() else {"runs": {}, "next_key": keys[0], "keys": keys}
        if occurrence in state["runs"]:
            return state["runs"][occurrence]
        next_key = state["next_key"]
        if next_key not in keys:
            old = state["keys"]
            start = old.index(next_key) if next_key in old else 0
            next_key = next((old[(start + i) % len(old)] for i in range(len(old))
                if old[(start + i) % len(old)] in keys), keys[0])
        start = keys.index(next_key)
        selected = [keys[(start + i) % len(keys)] for i in range(min(count, len(keys)))]
        run = {"occurrence": occurrence, "run_id": "rotation-" + digest(occurrence)[:24],
            "source_keys": selected, "inventory": keys, "inventory_sha256": digest(keys),
            "cursor": start, "created_at": now_stamp()}
        state.update(keys=keys, next_key=keys[(start + len(selected)) % len(keys)])
        state["runs"][occurrence] = run
        atomic_write_json(path, state)
        return run


def native_events(database: Path, session_id: str):
    """Only durable native cron transcript tool completions are review evidence."""
    with sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        session = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session or session["source"] != "cron" or not session_id.startswith("cron_"):
            raise ValueError("review requires a native Hermes cron session")
        calls, events = {}, []
        for row in connection.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,)):
            for call in json.loads(row["tool_calls"] or "[]"):
                function = call.get("function", call)
                arguments = function.get("arguments", {})
                calls[call["id"]] = {"tool": function.get("name"),
                    "arguments": json.loads(arguments) if isinstance(arguments, str) else arguments}
            if row["role"] == "tool" and row["tool_call_id"] in calls:
                events.append({**calls[row["tool_call_id"]], "result": row["content"],
                    "tool_call_id": row["tool_call_id"], "session_id": session_id})
        return dict(session), events


def readable_text(text):
    """Lossless short JSON fragments keep native read_file from clipping long lines."""
    return json.dumps([text[i:i + 500] for i in range(0, len(text), 500)], ensure_ascii=False, indent=2) + "\n"


def freeze_readable(path, text):
    if not any(len(line) > 500 for line in text.splitlines()):
        return None
    from climate_delivery.io import atomic_write_bytes
    value = readable_text(text).encode()
    atomic_write_bytes(path, value)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(value).hexdigest()}


def read_verified_text(events, path, text, view=None):
    if Path(path).read_bytes() != text.encode():
        raise ValueError("frozen text identity changed")
    if view:
        value = Path(view["path"]).read_bytes()
        if value != readable_text(text).encode() or hashlib.sha256(value).hexdigest() != view["sha256"]:
            raise ValueError("lossless review text identity changed")
        try:
            return {"raw_path": str(Path(path).resolve()), "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "inspection": read_evidence(events, Path(view["path"]), text=value.decode())}
        except ValueError:
            pass  # Historical receipts can still contain an actual complete raw read.
    return read_evidence(events, Path(path), text=text)


def read_evidence(events, path: Path, *, text: str | None = None, image=False):
    path = str(path.resolve())
    numbered_reads, covered = [], {}
    expected_lines = text.splitlines() if text is not None else []
    for event in events:
        args = event.get("arguments", {})
        values = [str(args.get(key) or "") for key in (("image_url",) if image else ("path", "file_path"))]
        if path not in values:
            continue
        tool = str(event.get("tool") or "").split(".")[-1]
        allowed = {"vision_analyze"} if image else {"read_file", "file_read"}
        result = str(event.get("result") or "")
        if tool not in allowed or not result.strip():
            continue
        try:
            decoded = json.loads(result)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict) and (decoded.get("error") or decoded.get("status") in {"failed", "error"}):
            continue
        if image and not (result.startswith("Image attached natively for the main model (")
            or result.startswith("Image loaded into your context — you can see it natively now. Use your built-in vision") and result.rstrip().endswith("[screenshot]")
            or isinstance(decoded, dict) and decoded.get("analysis") and not decoded.get("error")):
            continue
        if text is not None:
            body = decoded.get("content", decoded.get("output", result)) if isinstance(decoded, dict) else result
            if tool == "read_file" and isinstance(decoded, dict) and decoded.get("total_lines") == len(expected_lines):
                rows = [re.fullmatch(r"\s*(\d+)\|(.*)", line) for line in str(body).splitlines()]
                offset = args.get("offset", 1)
                if (decoded.get("truncated_lines") or not rows or not all(rows)
                    or [int(row[1]) for row in rows] != list(range(offset, offset + len(rows)))
                    or decoded.get("truncated_by") == "bytes" and decoded.get("next_offset") != offset + len(rows)):
                    continue
                if any(int(row[1]) > len(expected_lines) or row[2] != expected_lines[int(row[1]) - 1] for row in rows):
                    continue
                for row in rows:
                    covered[int(row[1])] = row[2]
                numbered_reads.append(event)
                if len(covered) == len(expected_lines):
                    return {"path": path, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "line_count": len(expected_lines), "events": numbered_reads}
                continue
            if text not in str(body):
                continue
        return event
    raise ValueError(f"actual {'image inspection' if image else 'full text read'} evidence is missing: {Path(path).name}")


def owner_finished(claim):
    owner, _ = native_events(Path(claim["hermes_database"]), claim["session_id"])
    return str(owner.get("end_reason") or "").startswith("cron_")


def acquisition_state(root: Path):
    """Read current owner and real writer receipts alongside business conclusions."""
    root = Path(root)
    state = json.loads((root / "state.json").read_text())
    claim_path = root / "claim.json"
    if claim_path.exists():
        state["claim"] = json.loads(claim_path.read_text())
    state["claim_failures"] = [json.loads(p.read_text()) for p in sorted((root / "claim-history").glob("*.json"))]
    state["writer_activations"] = []
    if (root / "activation.json").exists():
        request = json.loads((root / "activation.json").read_text())
        queue = Path(request["registry_snapshot_path"]).parents[2]
        for path in sorted((queue / "web").glob("*/request.json")):
            value = json.loads(path.read_text())
            if value.get("source_batch_id") == state["packet"]["batch_id"] and (path.parent / "status.json").exists():
                state["writer_activations"].append(json.loads((path.parent / "status.json").read_text()))
    return state


def claim_review(root: Path, packet: dict, *, session_id: str, execution_id: str,
    hermes_database: Path, reviewer: str, timeout_seconds=4500, now=None):
    session, _ = native_events(hermes_database, session_id)
    if not execution_id or not reviewer or timeout_seconds < 1:
        raise ValueError("native execution identity and positive timeout are required")
    at = now or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("claim time must be aware")
    path = Path(root) / "claim.json"
    if path.exists():
        old = json.loads(path.read_text())
        if old["packet_sha256"] == digest(packet) and old["session_id"] == session_id:
            if old.get("released_at"):
                raise ValueError("finished review requires another independent cron context")
            return old
        if not old.get("released_at"):
            owner, _ = native_events(Path(old["hermes_database"]), old["session_id"])
            if not str(owner.get("end_reason") or "").startswith("cron_"):
                raise ValueError("review is owned; expired claims require native owner completion evidence")
            atomic_write_json(Path(root) / "claim-history" / (old["token"] + ".json"),
                {**old, "status": "review_failed", "reason": "native cron ended without a review receipt",
                 "end_reason": owner["end_reason"], "reconciled_at": at.isoformat()})
    claim = {"token": secrets.token_hex(24), "packet_sha256": digest(packet),
        "session_id": session_id, "execution_id": execution_id, "reviewer": reviewer,
        "job_id": session_id.removeprefix("cron_").rsplit("_", 2)[0],
        "hermes_database": str(Path(hermes_database).resolve()), "session_source": session["source"],
        "created_at": at.isoformat(), "deadline": (at + timedelta(seconds=timeout_seconds)).isoformat()}
    atomic_write_json(path, claim)
    return claim


def validate_claim(root, packet, token, *, now=None, recovery_receipt=None):
    claim = json.loads((Path(root) / "claim.json").read_text())
    # Submission closes review ownership, but its exact recorded recovery action
    # may consume that token once. Other released claims remain invalid.
    released_recovery = (recovery_receipt is not None
        and recovery_receipt.get("claim") == {key: value for key, value in claim.items() if key != "released_at"}
        and any(value.get("status") == "recovering" for value in recovery_receipt.get("conclusions", {}).get("sources", {}).values()))
    if claim["token"] != token or claim["packet_sha256"] != digest(packet) or claim.get("released_at") and not released_recovery:
        raise ValueError("review claim does not match the current packet")
    if timestamp(claim["deadline"]) <= (now or datetime.now(timezone.utc)):
        raise ValueError("review claim expired; retain progress and reconcile the owner")
    _, events = native_events(Path(claim["hermes_database"]), claim["session_id"])
    return claim, events


def queue_acquisition_review(binding_path: Path, binding: dict, payload: dict, *, error=None):
    from .acquisition import load_acquisition_batch
    from .errors import RegistryInputError
    root = Path(binding_path).parent / "acquisition-review"
    with exclusive_lock(root, "review"):
        try:
            loaded = load_acquisition_batch(binding["registry_database"], binding["acquisition_batch_id"])
        except (KeyError, RegistryInputError, sqlite3.Error, OSError):
            loaded = {"items": [], "searches": []}
        candidates = []
        for item in loaded["items"]:
            if item["selection_status"] == "selected":
                body_path = root / "evidence" / (item["acquisition_item_id"] + ".txt")
                from climate_delivery.io import atomic_write_bytes
                atomic_write_bytes(body_path, (item.get("markdown_content") or "").encode())
                candidates.append({"identity": item, "candidate_sha256": digest(item), "body_path": str(body_path.relative_to(root)),
                    "readable_text": freeze_readable(body_path.with_suffix(".readable.json"), item.get("markdown_content") or "")})
        packet = {"schema_version": "climate-acquisition-review.v1", "run_id": binding["run_id"],
            "registry_database": binding["registry_database"],
            "batch_id": binding["acquisition_batch_id"], "attempt": binding["attempt"],
            "task_version": binding["task_version"], "binding_sha256": digest(binding),
            "source_keys": binding["source_keys"], "rotation": binding.get("rotation"),
            "candidates": candidates, "source_outcomes": payload.get("source_outcomes", []),
            "searches": loaded["searches"], "error": error,
            "attempt_result_path": str(Path(binding_path).parent / f"attempt-{binding['attempt']}-result.json"),
            "tool_provenance": str(Path(binding_path).parent / f"attempt-{binding['attempt']}-tool-provenance.json")}
        for outcome in packet["source_outcomes"]:
            artifact = Path(outcome.get("artifact_path") or "")
            if artifact.is_file():
                outcome["readable_text"] = freeze_readable(root / "evidence" / (digest(outcome) + ".readable.json"), artifact.read_text())
        present_sources = {row["source"] for row in packet["source_outcomes"]}
        packet["source_outcomes"] += [{"source": key, "status": "not_attempted", "coverage_status": "unknown",
            "failure_reason": error or "source outcome missing", "attempts": []} for key in binding["source_keys"] if key not in present_sources]
        state_path = root / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {"item_reviews": {}, "history": []}
        previous_candidates = {c["identity"]["acquisition_item_id"]: c for c in state.get("packet", {}).get("candidates", [])}
        if state.get("packet", {}).get("parent_context"):
            packet["parent_context"] = state["packet"]["parent_context"]
        for candidate in candidates:
            old = previous_candidates.get(candidate["identity"]["acquisition_item_id"])
            if old and digest(old["identity"]) == candidate["candidate_sha256"] and old.get("display"):
                candidate.update(display=old["display"], revision=old["revision"], corrected_at=old.get("corrected_at"), candidate_sha256=old["candidate_sha256"])
        revision = digest(packet)
        atomic_write_json(root / "packets" / (revision + ".json"), packet)
        current_sources = {row["source"]: digest(row) for row in packet["source_outcomes"]}
        state["sources"] = {key: value for key, value in state.get("sources", {}).items()
            if current_sources.get(key) == value.get("evidence_sha256")}
        current = {row["identity"]["acquisition_item_id"]: row["candidate_sha256"] for row in candidates}
        state["item_reviews"] = {key: review for key, review in state["item_reviews"].items()
            if current.get(key) == review["candidate_sha256"]}
        state.update(packet_sha256=revision, packet=packet, status="pending_review")
        atomic_write_json(state_path, state)
        return state


def correct_candidate(root: Path, item_id: str, changes: dict, *, reason: str):
    """A display correction retains raw acquisition evidence and sibling approvals."""
    root = Path(root)
    if not reason or not changes or set(changes) - {"title", "summary"} or any(not isinstance(v, str) or not v.strip() for v in changes.values()):
        raise ValueError("only nonempty derived title/summary corrections are supported")
    with exclusive_lock(root, "review"):
        state = json.loads((root / "state.json").read_text())
        candidate = next((c for c in state["packet"]["candidates"] if c["identity"]["acquisition_item_id"] == item_id), None)
        if not candidate:
            raise ValueError("candidate is not in this review packet")
        candidate["display"] = {**candidate.get("display", {}), **changes}
        candidate["revision"] = candidate.get("revision", 1) + 1
        candidate["corrected_at"] = now_stamp()
        candidate["candidate_sha256"] = digest([candidate["identity"], candidate["display"]])
        state["item_reviews"].pop(item_id, None)
        state["history"].append({"status": "candidate_corrected", "item_id": item_id, "reason": reason,
            "revision": candidate["revision"], "candidate_sha256": candidate["candidate_sha256"]})
        if (root / "claim.json").exists():
            claim = json.loads((root / "claim.json").read_text())
            state["packet"]["parent_context"] = claim["session_id"]
            atomic_write_json(root / "claim.json", {**claim, "released_at": now_stamp()})
        state.update(packet_sha256=digest(state["packet"]), status="pending_review")
        atomic_write_json(root / "packets" / (state["packet_sha256"] + ".json"), state["packet"])
        atomic_write_json(root / "state.json", state)
        return state


def review_acquisition(root: Path, token: str, conclusions: dict, *, now=None):
    root = Path(root)
    with exclusive_lock(root, "review"):
        state = json.loads((root / "state.json").read_text())
        packet = state["packet"]
        claim, events = validate_claim(root, packet, token, now=now)
        inspections, proposal_evidence = [], []
        reviews = conclusions.get("items", {})
        for candidate in packet["candidates"]:
            item = candidate["identity"]
            key = item["acquisition_item_id"]
            if key not in reviews:
                continue
            review = reviews[key]
            if review.get("candidate_sha256") != candidate["candidate_sha256"] or review.get("status") not in {"pass", "needs_correction", "rejected"}:
                raise ValueError("candidate conclusion differs from its precise identity")
            if not review.get("reason"):
                raise ValueError("review reason is required")
            if review["status"] == "pass":
                if not (item["processing_status"] == "complete" and item["fetch_status"] == "success"
                    and item["content_version_id"] and item["publication_date_evidence"] and item["date_status"] == "eligible"):
                    raise ValueError("failed/incomplete candidate cannot pass")
                inspected = read_verified_text(events, root / candidate["body_path"], item["markdown_content"], candidate.get("readable_text"))
                inspections.append({"acquisition_item_id": key, "candidate_sha256": candidate["candidate_sha256"], "event": inspected})
                review = {**review, "inspection_sha256": digest(inspected)}
            state["item_reviews"][key] = {**review, "approved_at": (now or datetime.now(timezone.utc)).isoformat(),
                "raw_candidate_sha256": digest(item), "display": candidate.get("display", {}),
                "candidate_revision": candidate.get("revision", 1),
                "corrected_at": candidate.get("corrected_at"),
                "reviewer": claim["reviewer"], "session_id": claim["session_id"], "run_id": packet["run_id"],
                "batch_id": packet["batch_id"], "task_version": packet["task_version"]}
        source_reviews = conclusions.get("sources", {})
        outcomes = {item["source"]: item for item in packet["source_outcomes"]}
        for key, review in source_reviews.items():
            outcome = outcomes.get(key)
            if not outcome or review.get("evidence_sha256") != digest(outcome) or not review.get("reason"):
                raise ValueError("source conclusion must cite its actual outcome")
            if review.get("status") not in {"passed_success", "verified_no_new", "needs_correction", "recovering", "restricted", "unresolved"}:
                raise ValueError("invalid source review status")
            if review["status"] == "verified_no_new" and (outcome.get("status") != "succeeded" or not outcome.get("attempts")):
                raise ValueError("empty results alone do not prove no new information")
            if review["status"] in {"passed_success", "verified_no_new"}:
                artifact = Path(outcome["artifact_path"])
                if hashlib.sha256(artifact.read_bytes()).hexdigest() != outcome["artifact_sha256"]:
                    raise ValueError("source evidence changed")
                inspected = read_verified_text(events, artifact, artifact.read_text(), outcome.get("readable_text"))
                inspections.append({"source": key, "evidence_sha256": digest(outcome), "event": inspected})
            if outcome.get("status") != "succeeded" and review["status"] in {"passed_success", "verified_no_new"}:
                raise ValueError("failed source cannot be approved")
        proposals = conclusions.get("proposals", [])
        for proposal in proposals:
            if proposal.get("kind") not in {"skill", "tool", "application_code"} or not all(proposal.get(k) for k in
                ("reason", "evidence", "verified_result", "reproduction", "future_version")):
                raise ValueError("improvement proposals require real validation evidence")
            if proposal.get("source_key") not in outcomes or proposal.get("cause") in {"temporary", "restricted", "no_new"}:
                raise ValueError("temporary/restricted/no-new outcomes are not development proposals")
            required = (("root_cause", "files", "suggested_change", "pr_title", "pr_description", "validation")
                if proposal["kind"] == "application_code" else
                ("official_entry", "steps", "scope", "before", "after") if proposal["kind"] == "skill" else
                ("owner", "affected_sources", "suggested_change", "validation"))
            if not all(proposal.get(key) for key in required):
                raise ValueError("proposal lacks concrete reproducible implementation details")
            cited_calls = proposal.get("tool_call_ids", [])
            actual = [event for event in events if event["tool_call_id"] in cited_calls]
            if not actual or len(actual) != len(set(cited_calls)) or not any(str(proposal["verified_result"]) in str(e["result"]) for e in actual):
                raise ValueError("proposal validation must cite actual completed native tool calls")
            proposal_evidence.extend(actual)
        receipt = {"claim": claim, "conclusions": conclusions, "recorded_at": now_stamp(), "events_sha256": digest(events),
            "inspections": inspections, "proposal_evidence": proposal_evidence}
        state["history"].append(receipt)
        merged_sources = {**state.get("sources", {}), **source_reviews}
        incomplete = any(c["identity"]["acquisition_item_id"] not in state["item_reviews"] for c in packet["candidates"])
        incomplete |= any(key not in merged_sources for key in packet["source_keys"])
        state.update(sources=merged_sources, proposals=proposals, status="pending_review" if incomplete else "reviewed_partial")
        atomic_write_json(root / "receipts" / (digest(receipt) + ".json"), receipt)
        atomic_write_json(root / "state.json", state)
        atomic_write_json(root / "claim.json", {**claim, "released_at": now_stamp()})
        return state


def claim_acquisition(root: Path, **context):
    root = Path(root)
    with exclusive_lock(root, "review"):
        state = json.loads((root / "state.json").read_text())
        if state["status"] not in {"pending_review", "reviewed_partial", "recovering", "reviewing"}:
            raise ValueError("no pending acquisition review")
        if state["packet"].get("parent_context") == context["session_id"]:
            raise ValueError("candidate correction requires another independent cron context")
        attempt = json.loads(Path(state["packet"]["attempt_result_path"]).read_text())
        runtime_path = root.parent / "runtime.json"
        runtime = json.loads(runtime_path.read_text()) if runtime_path.exists() else {}
        if attempt.get("run_id") != state["packet"]["run_id"] or attempt.get("attempt") != state["packet"]["attempt"] or not attempt.get("finished_at") or runtime.get("state") in {"running", "launching"}:
            raise ValueError("an acquiring/live batch cannot be reviewed")
        claim = claim_review(root, state["packet"], **context)
        state.update(status="reviewing", claim=claim)
        atomic_write_json(root / "state.json", state)
        return claim


def recover_acquisition(root: Path, token: str, service, *, now=None):
    root = Path(root)
    with exclusive_lock(root, "review"):
        state = json.loads((root / "state.json").read_text())
        receipt = next((value for value in reversed(state["history"])
            if value.get("claim", {}).get("token") == token), None) if state["status"] in {"pending_review", "reviewing", "reviewed_partial"} else None
        claim, events = validate_claim(root, state["packet"], token, now=now, recovery_receipt=receipt)
        binding = service.binding(state["packet"]["run_id"])
        if digest(binding) != state["packet"]["binding_sha256"]:
            raise ValueError("recovery binding differs from the reviewed frozen contract")
        retryable_sources = [row for row in state["packet"]["source_outcomes"]
            if row.get("coverage_status") != "rejected" and row.get("status") != "succeeded"]
        retryable_items = []
        acquisition_path = root.parent / f"attempt-{binding['attempt']}-acquisition.json"
        if acquisition_path.is_file():
            from .acquisition import load_acquisition_batch, unresolved_acquisition_items
            payload = json.loads(acquisition_path.read_text())
            stored = load_acquisition_batch(binding["registry_database"], binding["acquisition_batch_id"])
            if digest(payload) != stored["payload_sha256"]:
                raise ValueError("recovery acquisition differs from Registry-verified evidence")
            attempt = json.loads(Path(state["packet"]["attempt_result_path"]).read_text())
            if (attempt.get("run_id") == binding["run_id"] and attempt.get("attempt") == binding["attempt"]
                    and attempt.get("retryable") is True and attempt.get("finished_at")):
                retryable_items = unresolved_acquisition_items(payload)
                # A successful listing does not grant access to its articles.
                refusals = {"permission_denied", "auth_required", "blocked", "rejected",
                    "no_reviewed_profile", "no_reviewed_scope", "policy_disabled", "robots.denied", "robots.forbidden"}
                for item in retryable_items:
                    evidence = item["evidence"]
                    reasons = [evidence.get("failure_reason")]
                    for tried in evidence.get("attempts", []):
                        reasons.extend([tried.get("stop_reason"), tried.get("error_code")])
                        error = tried.get("error")
                        reasons.append(error.get("code") if isinstance(error, dict) else error)
                    if any(isinstance(reason, str) and reason in refusals for reason in reasons):
                        raise ValueError("article access refusal cannot be bypassed by recovery")
        if not retryable_sources and not retryable_items:
            raise ValueError("no recoverable source remains; access refusal cannot be bypassed")
        result = service.resume(binding["run_id"])
        state.update(status="recovering", recovery={"claim": claim, "result": result, "events_sha256": digest(events)})
        atomic_write_json(root / "state.json", state)
        atomic_write_json(root / "claim.json", {**claim, "released_at": now_stamp()})
        return result


def activate_approved(root: Path, *, queue_dir: Path, database: Path, repository_root: Path):
    """Queue only current approved candidates; the existing sole writer activates."""
    from .acquisition import load_acquisition_batch
    from .web_ingest_pipeline import _batch_items, _manifest_item, _job_dir, _status_path, read_web_activation_request, REVIEW_REQUEST_SCHEMA
    from .pdf_pipeline import _snapshot_registry, _external
    root = Path(root)
    queue_dir = _external(queue_dir, repository_root)
    database = _external(database, repository_root)
    with exclusive_lock(root, "review"):
        state = json.loads((root / "state.json").read_text())
        packet = state["packet"]
        loaded = {item["acquisition_item_id"]: item for item in load_acquisition_batch(database, packet["batch_id"])["items"]}
        approvals = {key: value for key, value in state["item_reviews"].items() if value["status"] == "pass"}
        if not approvals:
            return {"status": "no_approved_candidates"}
        for key, review in approvals.items():
            if key not in loaded or digest(loaded[key]) != review["raw_candidate_sha256"]:
                raise ValueError("changed candidate must be independently reviewed again")
        if any(review.get("display") for review in approvals.values()):
            from .persistent import _exclusive_database_lock
            from .acquisition import _open_database
            with _exclusive_database_lock(database):
                connection = _open_database(database, acquisition_writer=True)
                try:
                    with connection:
                        for key, review in approvals.items():
                            if review.get("display"):
                                item = loaded[key]
                                fields = {field: item.get(field) for field in ("title", "summary", "publication_date")}
                                fields["content"] = " ".join(str(item.get("markdown_content") or "").split())
                                fields.update(review["display"])
                                record_knowledge(connection, kind="article", entity_id=item["article_id"],
                                    source_kind=item["discovery_kind"], source_ref=key, fields=fields,
                                    evidence={"candidate_sha256": review["candidate_sha256"], "content_sha256": item["content_sha256"]},
                                    recorded_at=review["corrected_at"])
                finally:
                    connection.close()
        request_id = "review-" + digest([packet["batch_id"], approvals])[:32]
        job = _job_dir(queue_dir, request_id)
        with exclusive_lock(queue_dir, request_id):
            if not (job / "request.json").exists():
                job.mkdir(parents=True, exist_ok=True)
                snapshot = job / "registry.sqlite3"
                snapshot_sha = _snapshot_registry(database, snapshot)
                items = [dict(_manifest_item(item), review=approvals[item["acquisition_item_id"]])
                    for item in _batch_items(snapshot, packet["batch_id"], require_frozen=False)
                    if item["acquisition_item_id"] in approvals]
                if len(items) != len(approvals):
                    raise ValueError("approved subset lacks indexable evidence")
                request = {"schema_version": REVIEW_REQUEST_SCHEMA, "batch_id": request_id,
                    "source_batch_id": packet["batch_id"], "frozen_payload_sha256": state["packet_sha256"],
                    "registry_snapshot": "registry.sqlite3", "registry_sha256": snapshot_sha,
                    "web_items": items, "created_at": now_stamp()}
                atomic_write_json(job / "request.json", request)
                atomic_write_json(_status_path(queue_dir, request_id), {"batch_id": request_id,
                    "source_batch_id": packet["batch_id"], "stage": "queued", "acquisition_complete": True,
                    "indexed": False, "chat_ready": False, "attempts": 0, "error": None,
                    "created_at": request["created_at"], "updated_at": now_stamp()})
            request = read_web_activation_request(queue_dir, request_id)
            status_path = _status_path(queue_dir, request_id)
            if status_path.exists() and json.loads(status_path.read_text()).get("stage") == "failed":
                atomic_write_json(job / "retry.json", {"batch_id": request_id, "requested_at": now_stamp()})
            atomic_write_json(root / "activation.json", request)
            return request
