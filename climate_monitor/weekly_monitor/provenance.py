from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping


PROVENANCE_SCHEMA_VERSION = "weekly-monitor-provenance.v1"


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
    return {
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
