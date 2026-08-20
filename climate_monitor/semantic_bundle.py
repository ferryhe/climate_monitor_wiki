"""Deterministic, verifiable article semantic bundle sidecar producer.

The 08:00 weekly monitor selects its final articles in
``climate_monitor.orchestrator.run_monitor`` and renders them into one
canonical Markdown report. This module binds a second, machine-readable
artifact to that exact Markdown byte sequence: one validated
``{summary, categories, keywords}`` bundle per finally-selected article.

Design constraints encoded here:

* **Exactly one authoring pass.** Semantics are read from whatever the single
  existing Hermes/``web_listening`` pass already attached to the item
  (``CandidateItem.semantics``), or derived deterministically from the values
  the in-repository classifier already computed. This module never fetches,
  never opens a socket, and never calls a model.
* **Fail closed.** ``climate_monitor.taxonomy.validate_semantic_bundle`` is the
  runtime authority. Anything it rejects raises; nothing is silently repaired.
* **Deterministic bytes.** Stable ordering, stable key order, stable
  separators, trailing newline. Re-running produces byte-identical output.
* **No half-updates.** Markdown and sidecar are staged as ``.pending`` files
  and committed together; an interrupted commit is repaired by
  :func:`recover_pending_commit` before the next run writes anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .dedupe import canonical_url
from .taxonomy import ArticleTaxonomy, load_article_taxonomy, validate_semantic_bundle


SIDECAR_SCHEMA_VERSION = "article-semantic-sidecar.v1"
BUNDLE_SCHEMA_VERSION = "article-semantic-bundle.v1"
ARTICLE_IDENTITY_VERSION = "article-identity.v1"
GENERATOR = "climate_monitor.semantic_bundle"
SIDECAR_SUFFIX = ".semantics.json"
PENDING_SUFFIX = ".pending"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPORT_URL_LINE = re.compile(r"^\*\*URL:\*\* (\S+) <br>$", re.MULTILINE)
_LANE_ORDER = ("website", "document", "research")
_AGENT_BUNDLE_FIELDS = frozenset({"summary", "categories", "keywords"})
_BOUND_BUNDLE_FIELDS = _AGENT_BUNDLE_FIELDS | {
    "schema_version",
    "taxonomy_id",
    "taxonomy_sha256",
}
_ARTICLE_FIELDS = (
    "article_id",
    "position",
    "url",
    "canonical_url",
    "title",
    "source",
    "lane",
    "content_hash",
    "semantics",
    "semantics_provenance",
)


class SemanticBundleError(ValueError):
    """A semantic sidecar cannot be produced or verified as contract-valid."""


# ---------------------------------------------------------------------------
# Paths and identity
# ---------------------------------------------------------------------------


def semantic_sidecar_path(report_path: str | Path) -> Path:
    """Return the sidecar path bound to one canonical Markdown report."""

    path = Path(report_path)
    if not path.name.endswith(".md"):
        raise SemanticBundleError("canonical report must be a Markdown file")
    return path.with_name(path.name[: -len(".md")] + SIDECAR_SUFFIX)


def _pending_path(path: Path) -> Path:
    return path.with_name(path.name + PENDING_SUFFIX)


def article_identity(item: Any) -> str:
    """Return a stable, tracking-parameter-independent per-article identity."""

    canonical = canonical_url(str(_value(item, "url", "")))
    if not canonical.strip():
        raise SemanticBundleError("article has no canonical URL identity")
    digest = hashlib.sha256()
    digest.update(ARTICLE_IDENTITY_VERSION.encode("utf-8"))
    digest.update(b"\n")
    digest.update(canonical.encode("utf-8"))
    return digest.hexdigest()


def _value(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


# ---------------------------------------------------------------------------
# Semantics: one authoring pass, deterministic fallback, fail-closed validation
# ---------------------------------------------------------------------------


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _clean_values(values: Any) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in tuple(values or ()):
        cleaned = _clean(raw).strip(" ,;")
        key = cleaned.casefold()
        if cleaned and key not in seen:
            output.append(cleaned)
            seen.add(key)
    return output


def derive_semantic_bundle(item: Any, *, taxonomy: ArticleTaxonomy) -> dict[str, Any]:
    """Return one validated semantic bundle for a finally-selected article.

    If the single existing authoring pass supplied ``semantics`` on the item,
    it is used **verbatim** and must pass validation as authored. Otherwise the
    bundle is derived from values the pipeline already computed for this item.
    Either way the result must pass
    :func:`climate_monitor.taxonomy.validate_semantic_bundle`; nothing an author
    supplied is ever repaired.
    """

    return _validated_bundle(_candidate_bundle(item, taxonomy=taxonomy), taxonomy=taxonomy)


def semantics_provenance(item: Any) -> str:
    return "agent_bundle" if _value(item, "semantics", None) is not None else "pipeline_derived"


def _candidate_bundle(item: Any, *, taxonomy: ArticleTaxonomy) -> dict[str, Any]:
    supplied = _value(item, "semantics", None)
    if supplied is not None:
        return _agent_bundle(supplied)
    return _derived_bundle(item, taxonomy=taxonomy)


def _derived_bundle(item: Any, *, taxonomy: ArticleTaxonomy) -> dict[str, Any]:
    """Compose a bundle from values the pipeline already produced.

    This is construction, not repair: no authored text is edited. The
    composition rules are fixed and deterministic, and the validator still has
    the final say — if the composed bundle cannot satisfy the taxonomy
    constraints, the run fails closed.
    """

    constraints = taxonomy.constraints
    categories = _clean_values(_value(item, "categories", ()))
    categories = [label for label in categories if label in taxonomy.allowed_labels]
    categories = categories[: constraints.categories_max_items]

    keywords = _clean_values(_value(item, "keywords", ()) or _value(item, "topics", ()))
    # Mirror the long-standing report_writer fallback: category labels are
    # already-derived pipeline values and are the deterministic top-up source.
    if len(keywords) < constraints.keywords_min_items:
        keywords = _clean_values(keywords + [label.casefold() for label in categories])
    keywords = [
        keyword
        for keyword in keywords
        if keyword.casefold() not in constraints.disallowed_keywords
        and "," not in keyword
        and ";" not in keyword
        and len(keyword) <= constraints.keyword_max_chars
    ][: constraints.keywords_max_items]

    return {
        "summary": _clip(_clean(_value(item, "summary", "")), constraints.summary_max_chars),
        "categories": categories,
        "keywords": keywords,
    }


def _clip(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    head = value[:maximum].rstrip()
    boundary = head.rfind(" ")
    return (head[:boundary].rstrip() if boundary > 0 else head) or head


def _agent_bundle(supplied: Any) -> dict[str, Any]:
    if not isinstance(supplied, Mapping):
        raise SemanticBundleError("agent semantics must be an object")
    fields = set(supplied)
    if fields != _AGENT_BUNDLE_FIELDS and fields != _BOUND_BUNDLE_FIELDS:
        raise SemanticBundleError("agent semantics must contain exactly summary, categories, keywords")
    bundle = {name: supplied[name] for name in _AGENT_BUNDLE_FIELDS}
    for name in ("schema_version", "taxonomy_id", "taxonomy_sha256"):
        if name in supplied:
            bundle[name] = supplied[name]
    return bundle


def _validated_bundle(candidate: Mapping[str, Any], *, taxonomy: ArticleTaxonomy) -> dict[str, Any]:
    bound = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "taxonomy_id": taxonomy.taxonomy_id,
        "taxonomy_sha256": taxonomy.sha256,
        "summary": candidate.get("summary"),
        "categories": candidate.get("categories"),
        "keywords": candidate.get("keywords"),
    }
    for name in ("schema_version", "taxonomy_id", "taxonomy_sha256"):
        if name in candidate and candidate[name] != bound[name]:
            raise SemanticBundleError(f"agent semantics {name} does not match the configured taxonomy contract")
    for name in ("categories", "keywords"):
        if isinstance(bound[name], tuple):
            bound[name] = list(bound[name])
    try:
        return validate_semantic_bundle(bound, taxonomy=taxonomy)
    except ValueError as exc:
        raise SemanticBundleError(f"semantic bundle is not contract-valid: {exc}") from exc


# ---------------------------------------------------------------------------
# Sidecar payload
# ---------------------------------------------------------------------------


def render_order(items: Iterable[Any]) -> list[Any]:
    """Return the items in the exact order ``render_report`` emits them."""

    ordered = list(items)
    return [item for lane in _LANE_ORDER for item in ordered if _value(item, "lane") == lane]


def build_sidecar_payload(
    *,
    report_date: date,
    report_filename: str,
    report_sha256: str,
    items: Sequence[Any],
    taxonomy: ArticleTaxonomy | None = None,
) -> dict[str, Any]:
    selected = taxonomy or load_article_taxonomy()
    if _SHA256.fullmatch(str(report_sha256)) is None:
        raise SemanticBundleError("report sha256 must be a lowercase hex digest")
    ordered = render_order(items)
    if len(ordered) != len(list(items)):
        raise SemanticBundleError("every selected article must belong to a rendered lane")

    articles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, item in enumerate(ordered, start=1):
        identity = article_identity(item)
        if identity in seen:
            raise SemanticBundleError("selected articles must have unique canonical identities")
        seen.add(identity)
        articles.append(
            {
                "article_id": identity,
                "position": position,
                "url": str(_value(item, "url", "")),
                "canonical_url": canonical_url(str(_value(item, "url", ""))),
                "title": _clean(_value(item, "title", "")),
                "source": _clean(_value(item, "source_name", "")),
                "lane": str(_value(item, "lane", "")),
                "content_hash": str(_value(item, "content_hash", "")),
                "semantics": derive_semantic_bundle(item, taxonomy=selected),
                "semantics_provenance": semantics_provenance(item),
            }
        )

    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "article_count": len(articles),
        "articles": articles,
        "report": {
            "date": report_date.isoformat(),
            "filename": report_filename,
            "sha256": report_sha256,
        },
        "taxonomy": {
            "schema_version": selected.schema_version,
            "taxonomy_id": selected.taxonomy_id,
            "sha256": selected.sha256,
        },
        "provenance": {
            "generator": GENERATOR,
            "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
            "identity_scheme": ARTICLE_IDENTITY_VERSION,
        },
    }


def serialize_sidecar(payload: Mapping[str, Any]) -> bytes:
    """Serialize deterministically: sorted keys, fixed separators, trailing LF."""

    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    )
    if "\r" in text:
        raise SemanticBundleError("sidecar content must not contain carriage returns")
    return (text + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def rendered_article_urls(report_text: str) -> list[str]:
    return _REPORT_URL_LINE.findall(report_text)


def _verify_payload(payload: Any, *, report_bytes: bytes, report_path: Path) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SemanticBundleError("sidecar payload must be an object")
    if payload.get("schema_version") != SIDECAR_SCHEMA_VERSION:
        raise SemanticBundleError("unsupported sidecar schema_version")

    taxonomy = load_article_taxonomy()
    identity = payload.get("taxonomy")
    if not isinstance(identity, Mapping) or (
        identity.get("taxonomy_id") != taxonomy.taxonomy_id
        or identity.get("sha256") != taxonomy.sha256
        or identity.get("schema_version") != taxonomy.schema_version
    ):
        raise SemanticBundleError("sidecar taxonomy identity does not match the supported taxonomy")

    report = payload.get("report")
    if not isinstance(report, Mapping):
        raise SemanticBundleError("sidecar report identity is missing")
    actual_sha256 = hashlib.sha256(report_bytes).hexdigest()
    if report.get("sha256") != actual_sha256:
        raise SemanticBundleError("sidecar is not bound to the canonical report sha256")
    if report.get("filename") != report_path.name:
        raise SemanticBundleError("sidecar is not bound to the canonical report filename")

    articles = payload.get("articles")
    if not isinstance(articles, list) or payload.get("article_count") != len(articles):
        raise SemanticBundleError("sidecar article_count does not match its articles")

    rendered = rendered_article_urls(report_bytes.decode("utf-8"))
    if [article.get("url") for article in articles] != rendered:
        raise SemanticBundleError("sidecar articles do not correspond 1:1 with the canonical report")

    seen: set[str] = set()
    for position, article in enumerate(articles, start=1):
        if not isinstance(article, Mapping) or set(article) != set(_ARTICLE_FIELDS):
            raise SemanticBundleError("sidecar article does not match the article contract")
        if article.get("position") != position:
            raise SemanticBundleError("sidecar article ordering is not deterministic")
        expected_identity = article_identity(article)
        if article.get("article_id") != expected_identity:
            raise SemanticBundleError("sidecar article identity does not match its canonical URL")
        if expected_identity in seen:
            raise SemanticBundleError("sidecar contains a duplicate article identity")
        seen.add(expected_identity)
        if article.get("canonical_url") != canonical_url(str(article.get("url", ""))):
            raise SemanticBundleError("sidecar canonical URL is inconsistent")
        if article.get("semantics_provenance") not in {"agent_bundle", "pipeline_derived"}:
            raise SemanticBundleError("sidecar article provenance is not recognised")
        try:
            validate_semantic_bundle(article.get("semantics"), taxonomy=taxonomy)
        except ValueError as exc:
            raise SemanticBundleError(f"sidecar semantic bundle is not contract-valid: {exc}") from exc

    if serialize_sidecar(payload) != serialize_sidecar(json.loads(serialize_sidecar(payload))):
        raise SemanticBundleError("sidecar serialization is not stable")
    return dict(payload)


def verify_semantic_sidecar(report_path: str | Path) -> dict[str, Any]:
    """Verify the committed Markdown/sidecar pair, or raise."""

    path = Path(report_path)
    sidecar_path = semantic_sidecar_path(path)
    try:
        report_bytes = path.read_bytes()
    except OSError as exc:
        raise SemanticBundleError("canonical report is unavailable") from exc
    try:
        raw = sidecar_path.read_bytes()
    except OSError as exc:
        raise SemanticBundleError("semantic sidecar is missing") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SemanticBundleError("semantic sidecar is not valid JSON") from exc
    return _verify_payload(payload, report_bytes=report_bytes, report_path=path)


# ---------------------------------------------------------------------------
# Atomic two-file commit and crash recovery
# ---------------------------------------------------------------------------


def _write_pending(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except (OSError, AttributeError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _discard(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def commit_report_with_semantics(
    *,
    report_path: str | Path,
    report_date: date,
    report_text: str,
    items: Sequence[Any],
    taxonomy: ArticleTaxonomy | None = None,
) -> dict[str, Any]:
    """Validate, stage, and commit the canonical Markdown and its sidecar.

    Validation runs to completion *before* anything on disk is touched, so a
    contract failure can never overwrite an existing canonical artifact. Both
    files are then staged as ``.pending`` siblings and committed with
    ``os.replace``; an interruption between the two commits is repaired on the
    next :func:`recover_pending_commit` call.
    """

    path = Path(report_path)
    sidecar_path = semantic_sidecar_path(path)
    recover_pending_commit(path)

    report_bytes = report_text.encode("utf-8")
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    payload = build_sidecar_payload(
        report_date=report_date,
        report_filename=path.name,
        report_sha256=report_sha256,
        items=items,
        taxonomy=taxonomy,
    )
    sidecar_bytes = serialize_sidecar(payload)
    _verify_payload(payload, report_bytes=report_bytes, report_path=path)

    pending_report = _pending_path(path)
    pending_sidecar = _pending_path(sidecar_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_pending(pending_report, report_bytes)
        _write_pending(pending_sidecar, sidecar_bytes)
        _fsync_directory(path.parent)
        os.replace(pending_report, path)
        os.replace(pending_sidecar, sidecar_path)
    except BaseException:
        # Leave the staged files in place: recovery completes or discards them.
        raise
    _fsync_directory(path.parent)
    return {
        "report_path": path,
        "report_sha256": report_sha256,
        "sidecar_path": sidecar_path,
        "payload": payload,
    }


def recover_pending_commit(report_path: str | Path) -> str:
    """Complete or discard an interrupted commit.

    Returns ``"clean"``, ``"applied"``, or ``"discarded"``.
    """

    path = Path(report_path)
    sidecar_path = semantic_sidecar_path(path)
    pending_report = _pending_path(path)
    pending_sidecar = _pending_path(sidecar_path)

    if not pending_report.exists() and not pending_sidecar.exists():
        return "clean"
    if not pending_sidecar.exists():
        _discard(pending_report, pending_sidecar)
        return "discarded"

    try:
        payload = json.loads(pending_sidecar.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        _discard(pending_report, pending_sidecar)
        return "discarded"

    for source in (pending_report, path):
        try:
            report_bytes = source.read_bytes()
        except OSError:
            continue
        try:
            _verify_payload(payload, report_bytes=report_bytes, report_path=path)
        except SemanticBundleError:
            continue
        if source == pending_report:
            os.replace(pending_report, path)
        else:
            _discard(pending_report)
        os.replace(pending_sidecar, sidecar_path)
        _fsync_directory(path.parent)
        return "applied"

    _discard(pending_report, pending_sidecar)
    return "discarded"
