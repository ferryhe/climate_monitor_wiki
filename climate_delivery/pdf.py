"""Weekly PDF entrypoint; layout lives in the shared IAA CSC template."""
import hashlib
import json
from pathlib import Path
from typing import Any

from .delivery import _validate_summary
from .errors import GenerationError
from .templates import render_identity
from .templates import rendering_metadata
from .io import atomic_write_json
from .templates.adapters import adapt_weekly_report
from .templates.iaa_csc import render_report


def render_pdf(summary: dict[str, Any], output: Path, *, allow_offcycle: bool = False) -> None:
    _validate_summary(summary, allow_offcycle=allow_offcycle)
    try:
        render_report(adapt_weekly_report(summary), output)
    except GenerationError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise GenerationError("weekly PDF input is invalid") from exc


def ensure_weekly_report_pdf(summary: dict[str, Any], artifact_dir: Path, *, allow_offcycle: bool = False) -> Path:
    """Cache an independent render without replacing a canonical/mail artifact."""
    digest = hashlib.sha256(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    target = Path(artifact_dir) / "renders" / digest / render_identity() / f"climate-monitor-{summary['report']['date']}.pdf"
    if not target.is_file():
        render_pdf(summary, target, allow_offcycle=allow_offcycle)
        atomic_write_json(target.parent / "manifest.json", {
            "schema_version": "climate-pdf-render.v1",
            "input": {"report_sha256": summary["report"]["sha256"], "summary_sha256": digest},
            "rendering": rendering_metadata(),
            "pdf": {"path": target.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()},
        })
    return target
