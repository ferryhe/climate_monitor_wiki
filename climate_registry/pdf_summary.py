"""Explicit adoption of exact original PDF summary text, separate from fact verification."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .acquisition_review import digest, now_stamp
from .errors import RegistryInputError

ORIGINAL_SUMMARY_LABEL = "Adopted from the original PDF"
_LEGACY_SUMMARY_LABEL = "采用原文"


def original_summary(document):
    text = document.get("executive_summary")
    if not isinstance(text, str) or not text.strip():
        raise RegistryInputError("original PDF executive summary is empty")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if sha != document.get("executive_summary_sha256"):
        raise RegistryInputError("original PDF executive summary hash differs")
    return text, sha


def adopted_summary(connection, document_sha256, document):
    empty = {"executive_summary": []}
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='pdf_intake_summary_adoptions'").fetchone():
        return empty
    try:
        text, sha = original_summary(document)
    except RegistryInputError:
        return empty
    row = connection.execute("SELECT adoption_id,receipt_json FROM pdf_intake_summary_adoptions WHERE document_sha256=? AND executive_summary_sha256=?",
        (document_sha256, sha)).fetchone()
    if row is None:
        return empty
    receipt = json.loads(row[1])
    if not isinstance(receipt, dict):
        return empty
    if (digest(receipt) != row[0] or receipt.get("document_sha256") != document_sha256
            or receipt.get("executive_summary_sha256") != sha or receipt.get("display_label") not in (ORIGINAL_SUMMARY_LABEL, _LEGACY_SUMMARY_LABEL)):
        return empty
    return {"executive_summary": [text], "executive_summary_basis": "adopted_original_pdf",
        "executive_summary_label": ORIGINAL_SUMMARY_LABEL, "executive_summary_reference": {
            "document_sha256": document_sha256, "executive_summary_sha256": sha, "adoption_id": row[0]}}


def public_adopted_summary(payload):
    """Validate the exact original-text exception when reading a public artifact."""
    parts, reference = payload.get("executive_summary"), payload.get("executive_summary_reference")
    if (payload.get("executive_summary_basis") != "adopted_original_pdf"
            or payload.get("executive_summary_label") not in (ORIGINAL_SUMMARY_LABEL, _LEGACY_SUMMARY_LABEL)
            or not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], str)
            or not parts[0].strip() or not isinstance(reference, dict)
            or reference.get("document_sha256") != payload.get("document_sha256")
            or hashlib.sha256(parts[0].encode()).hexdigest() != reference.get("executive_summary_sha256")
            or not reference.get("adoption_id")):
        return {"executive_summary": []}
    return {"executive_summary": parts, "executive_summary_basis": "adopted_original_pdf",
        "executive_summary_reference": reference, "executive_summary_label": ORIGINAL_SUMMARY_LABEL}


def adopt_pdf_summary(database, document_sha256, expected_summary_sha256, *, actor, reason, backup_dir, apply=False):
    from .publication import resolve_database
    from .persistent import _exclusive_database_lock, _backup_connection, _backup_name
    from .contract import validate_registry_contract
    if not actor.strip() or not reason.strip():
        raise RegistryInputError("original-text adoption requires actual actor and authorization reason")
    database = resolve_database(database)
    with _exclusive_database_lock(database):
        with sqlite3.connect(database if apply else f"{database.as_uri()}?mode=ro", uri=not apply) as connection:
            if validate_registry_contract(connection) < 23:
                raise RegistryInputError("summary adoption requires schema23; run migrate-publication --apply")
            row = connection.execute("SELECT document_json FROM pdf_intake_documents WHERE document_sha256=?", (document_sha256,)).fetchone()
            if row is None:
                raise RegistryInputError("original PDF document not found")
            _text, sha = original_summary(json.loads(row[0]))
            if sha != expected_summary_sha256:
                raise RegistryInputError("expected original summary hash differs")
            existing = connection.execute("SELECT adoption_id FROM pdf_intake_summary_adoptions WHERE document_sha256=? AND executive_summary_sha256=?", (document_sha256, sha)).fetchone()
            if existing:
                return {"status": "no-op", "adoption_id": existing[0]}
            receipt = {"document_sha256": document_sha256, "executive_summary_sha256": sha,
                "adopted_at": now_stamp(), "actor": actor, "authorization_reason": reason,
                "display_label": ORIGINAL_SUMMARY_LABEL, "basis": "adopted_original_pdf"}
            identity = digest(receipt)
            if apply:
                backup_root = Path(backup_dir); backup_root.mkdir(parents=True, exist_ok=True)
                backup = backup_root / _backup_name(database)
                _backup_connection(connection, backup)
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("INSERT INTO pdf_intake_summary_adoptions VALUES(?,?,?,?,?,?)",
                    (identity, document_sha256, sha, receipt["adopted_at"], receipt["basis"], json.dumps(receipt, ensure_ascii=False, sort_keys=True)))
            return {"status": "adopted" if apply else "dry_run", "adoption_id": identity,
                "document_sha256": document_sha256, "executive_summary_sha256": sha,
                "display_label": ORIGINAL_SUMMARY_LABEL, "fact_verification": "not_asserted"}
