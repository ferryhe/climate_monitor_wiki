from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


PROVENANCE_SCHEMA_VERSION = "weekly-monitor-provenance.v1"
_JOB_ROOT = Path(__file__).resolve().parents[2] / "monitoring" / "jobs" / "weekly-climate-monitor-08h"
_PROVENANCE_SCHEMA_PATH = _JOB_ROOT / "contracts" / "provenance.v1.schema.json"


def require_complete_provenance_inputs(
    *,
    prompt_provenance: Mapping[str, str] | None,
    driver_version: str,
    contract_version: str,
    repository_commit_sha: str,
    model_metadata: Mapping[str, Any] | None,
) -> bool:
    requested = (
        prompt_provenance is not None
        or bool(driver_version)
        or bool(contract_version)
        or bool(repository_commit_sha)
        or model_metadata is not None
    )
    if not requested:
        return False

    missing: list[str] = []
    if prompt_provenance is None:
        missing.append("prompt_provenance")
    else:
        for field in ("id", "version", "sha256"):
            if not str(prompt_provenance.get(field, "")).strip():
                missing.append(f"prompt_provenance.{field}")
    if not driver_version:
        missing.append("driver_version")
    if not contract_version:
        missing.append("contract_version")
    if not repository_commit_sha:
        missing.append("repository_commit_sha")
    if model_metadata is None:
        missing.append("model_metadata")
    else:
        for field in ("provider", "model", "settings"):
            if field not in model_metadata:
                missing.append(f"model_metadata.{field}")
        if "settings" in model_metadata and not isinstance(model_metadata["settings"], Mapping):
            missing.append("model_metadata.settings")

    if missing:
        raise ValueError("provenance metadata is incomplete: missing " + ", ".join(missing))
    return True


@lru_cache(maxsize=1)
def _provenance_validator() -> Draft202012Validator:
    return Draft202012Validator(
        json.loads(_PROVENANCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    )


def build_run_provenance(
    *,
    commit: Mapping[str, Any],
    prompt_provenance: Mapping[str, str] | None,
    driver_version: str,
    contract_version: str,
    repository_commit_sha: str,
    model_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = commit["payload"]
    sidecar_path = Path(commit["sidecar_path"])
    sidecar_sha256 = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
    articles = payload["articles"]
    provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "repository": {"commit_sha": repository_commit_sha},
        "prompt": dict(prompt_provenance or {}),
        "driver": {
            "version": driver_version,
            "contract_version": contract_version,
        },
        "taxonomy": {
            "taxonomy_id": payload["taxonomy"]["taxonomy_id"],
            "sha256": payload["taxonomy"]["sha256"],
        },
        "report": {
            "filename": payload["report"]["filename"],
            "sha256": payload["report"]["sha256"],
        },
        "semantic_sidecar": {
            "filename": sidecar_path.name,
            "sha256": sidecar_sha256,
        },
        "final_articles": {
            "count": len(articles),
            "identities": [article["article_id"] for article in articles],
        },
        "model": dict(model_metadata or {"provider": "", "model": "", "settings": {}}),
    }
    _provenance_validator().validate(provenance)
    return provenance
