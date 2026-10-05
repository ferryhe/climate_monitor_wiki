"""Render the Registry's article-first Wiki projection."""

from __future__ import annotations

import os
import re
import json
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .persistent import _file_sha256, _read_only_connection, _validate_database
from .read_api import RegistryReader


OccurrenceFilter = Callable[[dict], bool]


def _write_if_changed(path: Path, content: str) -> str:
    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            return "unchanged"
        path.write_text(content, encoding="utf-8")
        return "updated"
    path.write_text(content, encoding="utf-8")
    return "created"


def _citation(occurrence: dict) -> str:
    files = sorted(
        {
            item.get("filename", "")
            for item in occurrence.get("source_observations", [])
            if item.get("filename")
        }
    )
    filename = ", ".join(files) or "unknown PDF"
    sha256 = occurrence.get("source_document_sha256", "unknown")
    page = occurrence.get("page", "unknown")
    url = occurrence.get("raw_url", "")
    link = f"; [original link]({url})" if url else ""
    return f"PDF: {filename}; SHA-256: {sha256}; page {page}{link}"


def _pdf_heading(occurrence: dict) -> str:
    return f"PDF report observation: {occurrence.get('occurrence_id') or 'unknown'}"


def _render_registry_article(article: dict) -> str | None:
    content = article.get("available_content") or article.get("content", {})
    report_summary = article.get("report_summary")
    appearances = article.get("appearances", [])
    pdf_occurrences = article.get("pdf_occurrences", [])
    acquisition_observations = article.get("acquisition_observations", [])
    date_observations = article.get("date_observations", [])
    current_version_has_appearance = any(
        appearance.get("version_id") == article.get("current_version_id")
        and appearance.get("summary") == report_summary
        for appearance in appearances
    )
    if not (
        content.get("markdown")
        or content.get("supporting_excerpt")
        or article.get("summary")
        or report_summary
        or acquisition_observations
        or date_observations
        or any(item.get("summary") for item in pdf_occurrences)
        or any(item.get("summary") for item in appearances)
    ):
        return None

    title = article.get("title") or next(
        (item.get("title") for item in acquisition_observations if item.get("title")),
        article["article_id"],
    )
    blocks = [
        f"# {title}",
        "",
        f"Canonical article: [{article['canonical_url']}]({article['canonical_url']})",
    ]
    if article.get("collected_at"):
        blocks.append(f"Collected at: {article['collected_at']}")
    if date_observations:
        blocks.extend(["", "## Date observations", ""])
        for observation in date_observations:
            evidence = observation["evidence"]
            label = ("Collection time" if observation["kind"] == "collection"
                     else "Page information date")
            detail = (
                f"{label}: {observation['observed_at']}; source record: "
                f"{evidence['source_system']}.{evidence['table']} {evidence['record_id']}; "
                f"match: {evidence['match_basis']}"
            )
            if evidence.get("date_kind"):
                detail += f"; page date kind: {evidence['date_kind']}"
            if evidence.get("source_url"):
                detail += f"; source URL: {evidence['source_url']}"
            detail += f"; evidence imported at: {observation['recorded_at']}"
            blocks.append(f"- {detail}")
    if article.get("categories"):
        blocks.extend(["", f"Categories: {', '.join(article['categories'])}"])
    if article.get("keywords"):
        blocks.extend(["", f"Keywords: {', '.join(article['keywords'])}"])
    if content.get("markdown") or content.get("supporting_excerpt"):
        blocks.extend(
            [
                "",
                "## Verified article content",
                "",
                content.get("markdown") or content["supporting_excerpt"],
                "",
                f"Article citation: [{article['canonical_url']}]({article['canonical_url']})",
            ]
        )
        if content.get("content_version_id"):
            blocks.append(f"Registry content version: {content['content_version_id']}")
        if content.get("content_sha256"):
            blocks.append(f"Content SHA-256: {content['content_sha256']}")
        if content.get("acquisition_item_id"):
            blocks.append(
                f"Acquisition observation: {content['acquisition_item_id']}; "
                f"fetch: {content.get('fetch_id') or 'unknown'}"
            )
    if article.get("summary"):
        semantic_content_version = article.get("enrichment", {}).get("content_version_id")
        blocks.extend(
            [
                "",
                "## Article semantic summary",
                "",
                article["summary"],
                "",
                f"Provenance: {article.get('summary_provenance') or 'unknown'}; "
                f"content version: {semantic_content_version or 'unknown'}; "
                f"article citation: [{article['canonical_url']}]({article['canonical_url']})",
            ]
        )
    if report_summary and not current_version_has_appearance:
        blocks.extend(
            [
                "",
                "## Registry article-version summary",
                "",
                report_summary,
                "",
                "Provenance: Registry article version; Registry article version: "
                f"{article.get('current_version_id') or 'unknown'}; Article citation: "
                f"[{article['canonical_url']}]({article['canonical_url']})",
            ]
        )
    for appearance in appearances:
        if appearance.get("summary"):
            blocks.extend(
                [
                    "",
                    f"## Report observation: {appearance['source_filename']}",
                    "",
                    appearance["summary"],
                    "",
                    f"Report citation: {appearance['source_filename']}; SHA-256: "
                    f"{appearance['source_sha256']}; [original link]({appearance['original_url']})",
                ]
            )
    for occurrence in pdf_occurrences:
        if occurrence.get("summary"):
            blocks.extend(
                [
                    "",
                    f"## {_pdf_heading(occurrence)}",
                    "",
                    occurrence["summary"],
                    "",
                    _citation(occurrence),
                ]
            )
    for observation in acquisition_observations:
        observation_blocks = [
            "",
            f"## Acquisition observation: {observation['acquisition_item_id']}",
            "",
            observation.get("summary") or "No summary was stored for this observation.",
            "",
            f"Provenance: {observation['discovery_kind']} observation; source: "
            f"{observation['source_name']}; discovered: {observation['discovered_at']}; "
            f"reference: {observation['discovery_ref']}; [original link]({observation['raw_url']})",
            f"Batch: {observation['batch_id']}; fetch: {observation['fetch_id']}; content version: "
            f"{observation.get('content_version_id') or observation.get('resolved_fetch', {}).get('content_version_id') or 'none'}; "
            f"status: {observation['processing_status']}/{observation['material_status']}",
        ]
        if observation.get("collected_at"):
            observation_blocks.append(f"Collected at: {observation['collected_at']}")
        if observation.get("publication_date"):
            observation_blocks.append(
                f"Publication date: {observation['publication_date']}"
            )
        if observation.get("publication_date_evidence"):
            observation_blocks.append(
                "Publication-date evidence: "
                + json.dumps(
                    observation["publication_date_evidence"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        blocks.extend(observation_blocks)
        for origin in observation.get("origins", []):
            search = origin.get("search") or {}
            search_detail = (
                f"; search: {search['search_id']} ({search['engine']}, {search['status']}, "
                f"{search['attempted_at']})"
                if search
                else ""
            )
            blocks.append(
                f"- Origin ({origin.get('discovery_kind', 'unknown')}): "
                f"{origin.get('source', 'unknown')}; {origin.get('discovered_at', 'unknown')}; "
                f"{origin.get('discovery_ref', 'unknown')}; "
                f"[link]({origin.get('url', observation['raw_url'])}){search_detail}"
            )
    blocks.append("")
    return "\n".join(blocks)


def _render_registry_source_observations(items: list[dict]) -> str | None:
    blocks = [
        "# Registry report source observations",
        "",
        "These are report-provided summaries; article details are unconfirmed.",
    ]
    kept = 0
    for item in items:
        for occurrence in item.get("occurrences", []):
            if not occurrence.get("summary"):
                continue
            kept += 1
            blocks.extend(
                [
                    "",
                    f"## {_pdf_heading(occurrence)}",
                    "",
                    occurrence["summary"],
                    "",
                    "Report-provided summary; article details are unconfirmed.",
                    _citation(occurrence),
                ]
            )
    return "\n".join([*blocks, ""]) if kept else None


def _json_strings(value: str | None) -> list[str]:
    parsed = json.loads(value) if value else []
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise RuntimeError("pinned Registry enrichment is invalid")
    return parsed


def _pinned_web_article(
    reader: RegistryReader, identities: list[dict[str, Any]]
) -> dict[str, Any]:
    article_id = identities[0]["article_id"]
    article = reader.article(article_id)
    expected = {item["acquisition_item_id"]: item for item in identities}
    observations = [
        item
        for item in article.get("acquisition_observations", [])
        if item["acquisition_item_id"] in expected
    ]
    if len(observations) != len(expected):
        raise RuntimeError("active web projection identity is missing")
    for observation in observations:
        identity = expected[observation["acquisition_item_id"]]
        bound_fields = ("batch_id", "content_version_id", "publication_date")
        if "collected_at" in identity:
            bound_fields += ("collected_at",)
        if any(
            observation.get(key) != identity[key]
            for key in bound_fields
        ) or observation.get("publication_date_evidence") != identity["publication_date_evidence"]:
            raise RuntimeError("active web projection identity changed")

    selected = max(
        observations,
        key=lambda item: (item.get("discovered_at") or "", item["acquisition_item_id"]),
    )
    version_id = selected["content_version_id"]
    with reader.connect() as connection:
        content = connection.execute(
            """SELECT content_version_id, content_sha256, markdown_content, content_type,
                      source_bytes, extraction_method, extraction_version, first_fetched_at
               FROM article_content_versions
               WHERE article_id=? AND content_version_id=?""",
            (article_id, version_id),
        ).fetchone()
        enrichment = connection.execute(
            """SELECT summary, categories_json, keywords_json, language, generator_kind,
                      generator_name, generator_version, generated_at
               FROM article_enrichments
               WHERE content_version_id=? AND status='complete'
               ORDER BY generated_at DESC, enrichment_id DESC LIMIT 1""",
            (version_id,),
        ).fetchone()
    if content is None:
        raise RuntimeError("active web projection content version is missing")

    pinned_content = {
        "content_version_id": version_id,
        "content_sha256": content["content_sha256"],
        "content_type": content["content_type"],
        "source_bytes": content["source_bytes"],
        "extraction_method": content["extraction_method"],
        "extraction_version": content["extraction_version"],
        "fetched_at": content["first_fetched_at"],
        "collected_at": selected.get("collected_at"),
        "acquisition_item_id": selected["acquisition_item_id"],
        "fetch_id": selected.get("fetch_id"),
        "selection_basis": "active_intake_manifest",
    }
    if article["display_policy"] == "full_markdown":
        pinned_content["markdown"] = content["markdown_content"]
    elif article["display_policy"] == "summary_excerpt":
        pinned_content["supporting_excerpt"] = " ".join(
            content["markdown_content"].split()
        )[:500]

    article.update(
        title=selected.get("title") or article["canonical_url"],
        report_summary=None,
        appearances=[],
        acquisition_observations=observations,
        collected_at=selected.get("collected_at"),
        pdf_occurrences=[],
        content=pinned_content,
        available_content=pinned_content,
    )
    if enrichment is not None:
        article.update(
            summary=enrichment["summary"],
            summary_provenance="content_enrichment",
            categories=_json_strings(enrichment["categories_json"]),
            keywords=_json_strings(enrichment["keywords_json"]),
            enrichment={
                "summary": enrichment["summary"],
                "categories": _json_strings(enrichment["categories_json"]),
                "keywords": _json_strings(enrichment["keywords_json"]),
                "language": enrichment["language"],
                "content_version_id": version_id,
                "generator": {
                    "kind": enrichment["generator_kind"],
                    "name": enrichment["generator_name"],
                    "version": enrichment["generator_version"],
                    "generated_at": enrichment["generated_at"],
                },
            },
        )
    else:
        article.update(
            summary=selected.get("summary"),
            summary_provenance="registry_acquisition",
            categories=[],
            keywords=[],
            enrichment={},
        )
    return article


def _pinned_pdf_title(occurrences: list[dict[str, Any]], canonical_url: str) -> str:
    for occurrence in occurrences:
        title = occurrence.get("anchor_text") or occurrence.get("title")
        if title:
            return str(title)
    return next(
        (str(item["raw_url"]) for item in occurrences if item.get("raw_url")),
        canonical_url,
    )


def render_runtime_registry(
    wiki_dir: Path,
    *,
    web_database: Path | None,
    pdf_database: Path | None,
    manifest: dict[str, Any],
) -> dict[str, str]:
    """Render only identities pinned by one validated active intake manifest."""
    wiki_dir.mkdir(parents=True, exist_ok=True)
    articles: dict[str, dict[str, Any]] = {}
    web_items = list(manifest.get("web_items", []))
    by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in web_items:
        by_article[item["article_id"]].append(item)
    if by_article:
        if web_database is None:
            raise RuntimeError("active web Registry snapshot is missing")
        web_reader = RegistryReader(
            web_database, repository_root=Path(__file__).resolve().parents[1]
        )
        for identities in by_article.values():
            article = _pinned_web_article(web_reader, identities)
            articles[article["article_id"]] = article

    pdf_ids = {str(value) for value in manifest.get("pdf_occurrence_ids", [])}
    source_observations: list[dict[str, Any]] = []
    if pdf_ids:
        if pdf_database is None:
            raise RuntimeError("active PDF Registry snapshot is missing")
        pdf_reader = RegistryReader(
            pdf_database, repository_root=Path(__file__).resolve().parents[1]
        )
        with pdf_reader.connect() as connection:
            rows = connection.execute(
                """SELECT o.occurrence_id, p.article_id AS pdf_article_id,
                          p.core_article_id
                   FROM pdf_intake_article_occurrences o
                   JOIN pdf_intake_articles p ON p.article_id=o.article_id"""
            ).fetchall()
        selected = [row for row in rows if row["occurrence_id"] in pdf_ids]
        if {row["occurrence_id"] for row in selected} != pdf_ids:
            raise RuntimeError("active PDF projection identity is missing")
        # Historical imports saved calendar links as article observations. Keep
        # those rows intact, but route their content to the meeting projection.
        with pdf_reader.connect() as connection:
            calendar_links = {row[0] for row in connection.execute(
                "SELECT occurrence_id FROM pdf_intake_article_occurrences WHERE json_extract(occurrence_json,'$.summary_basis')='verbatim_pdf_calendar_row'")}
        selected = [row for row in selected if row["occurrence_id"] not in calendar_links]
        confirmed: dict[str, set[str]] = defaultdict(set)
        unconfirmed: dict[str, set[str]] = defaultdict(set)
        for row in selected:
            target = confirmed if row["core_article_id"] else unconfirmed
            target[row["core_article_id"] or row["pdf_article_id"]].add(
                row["occurrence_id"]
            )
        for article_id, occurrence_ids in confirmed.items():
            detail = pdf_reader.article(article_id)
            occurrences = [
                item
                for item in detail.get("pdf_occurrences", [])
                if item.get("occurrence_id") in occurrence_ids
            ]
            if len(occurrences) != len(occurrence_ids):
                raise RuntimeError("active PDF projection occurrence is missing")
            if article_id in articles:
                articles[article_id]["pdf_occurrences"] = occurrences
            else:
                detail.update(
                    title=_pinned_pdf_title(occurrences, detail["canonical_url"]),
                    summary=None,
                    summary_provenance=None,
                    categories=[],
                    keywords=[],
                    enrichment={},
                    report_summary=None,
                    appearances=[],
                    acquisition_observations=[],
                    pdf_occurrences=occurrences,
                    content={},
                    available_content={},
                )
                articles[article_id] = detail
        for pdf_article_id, occurrence_ids in unconfirmed.items():
            detail = pdf_reader.pdf_article(pdf_article_id)
            detail["occurrences"] = [
                item
                for item in detail.get("occurrences", [])
                if item.get("occurrence_id") in occurrence_ids
            ]
            if len(detail["occurrences"]) != len(occurrence_ids):
                raise RuntimeError("active PDF source observation is missing")
            detail["title"] = _pinned_pdf_title(
                detail["occurrences"], detail["canonical_url"]
            )
            source_observations.append(detail)

    pages: dict[str, str] = {}
    for article_id, article in sorted(articles.items()):
        rendered = _render_registry_article(article)
        if rendered:
            pages[f"article-{article_id}.md"] = rendered
    rendered_sources = _render_registry_source_observations(source_observations)
    if rendered_sources:
        pages["registry-source-observations.md"] = rendered_sources
    calendar_ids = set(manifest.get("pdf_calendar_occurrence_ids", []))
    if calendar_ids:
        if pdf_database is None:
            raise RuntimeError("active meeting Registry snapshot is missing")
        meeting_reader = RegistryReader(pdf_database, repository_root=Path(__file__).resolve().parents[1])
        records = []
        cursor = 1
        while True:
            result = meeting_reader.pdf_calendar_items(page=cursor, page_size=100)
            records.extend(item for item in result["items"] if item["occurrence_id"] in calendar_ids)
            if cursor >= result["pagination"]["pages"]:
                break
            cursor += 1
        if {item["occurrence_id"] for item in records} != calendar_ids:
            raise RuntimeError("active PDF calendar observation is missing")
        blocks = ["# Meetings and key dates", ""]
        for item in records:
            candidate = item.get("collected_candidate") or {}
            blocks.extend([f"## {item['name']}", _citation(item),
                "PDF calendar observation: " + item["occurrence_id"],
                "Date(s): " + str(item.get("raw_date") or "Not provided"),
                "Host: " + str(item.get("organizer") or "Not provided"),
                "Relevance (PDF author): " + str(item.get("relevance_reason") or "Not provided"),
                "Verification: " + item["verification_status"],
                *[f"Collected {field}: {candidate[field]}" for field in
                    ("raw_time_text", "timezone", "location", "online_url", "status") if candidate.get(field)],
                *item.get("source_urls", []), ""])
        pages["registry-meetings.md"] = "\n".join(blocks) + "\n"
    for name, content in pages.items():
        (wiki_dir / name).write_text(content, encoding="utf-8")
    return pages


def _filtered(items: list[dict], occurrence_filter: OccurrenceFilter | None) -> list[dict]:
    return items if occurrence_filter is None else [item for item in items if occurrence_filter(item)]


def _registry_pages(
    database: Path,
    wiki_dir: Path,
    *,
    occurrence_filter: OccurrenceFilter | None = None,
) -> dict[str, str]:
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])
    pages: dict[str, str] = {}
    page = 1
    while True:
        payload = reader.articles(page=page, page_size=100)
        for item in payload["items"]:
            if item["document_kind"] != "article" or not item["publication_eligible"]:
                continue
            article = reader.article(item["article_id"])
            article["pdf_occurrences"] = _filtered(
                article.get("pdf_occurrences", []), occurrence_filter
            )
            rendered = _render_registry_article(article)
            if rendered:
                pages[f"article-{article['article_id']}.md"] = rendered
        if page >= payload["pagination"]["pages"]:
            break
        page += 1
    page = 1
    pdf_items: list[dict] = []
    while True:
        payload = reader.pdf_articles(page=page, page_size=100)
        for item in payload["items"]:
            article = reader.pdf_article(item["article_id"])
            article["occurrences"] = _filtered(article.get("occurrences", []), occurrence_filter)
            pdf_items.append(article)
        if page >= payload["pagination"]["pages"]:
            break
        page += 1
    rendered = _render_registry_source_observations(pdf_items)
    if rendered:
        pages["registry-source-observations.md"] = rendered
    states = {name: _write_if_changed(wiki_dir / name, content) for name, content in pages.items()}
    for existing in wiki_dir.glob("article-*.md"):
        if (
            re.fullmatch(r"article-[A-Za-z0-9_-]+\.md", existing.name)
            and existing.name not in pages
        ):
            existing.unlink()
            states[existing.name] = "deleted"
    source_only = wiki_dir / "registry-source-observations.md"
    if not rendered and source_only.exists():
        source_only.unlink()
        states[source_only.name] = "deleted"
    return states


def sync_registry_wiki(
    database: Path,
    wiki_dir: Path,
    *,
    occurrence_filter: OccurrenceFilter | None = None,
) -> dict[str, str]:
    """Project Registry records into an explicitly selected Wiki directory."""
    wiki_dir.mkdir(parents=True, exist_ok=True)
    return _registry_pages(database, wiki_dir, occurrence_filter=occurrence_filter)


def snapshot_registry(database: Path, destination: Path) -> str:
    """Create and validate one transactionally consistent SQLite snapshot."""
    database = database.resolve(strict=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    source = _read_only_connection(database)
    target = sqlite3.connect(temporary)
    try:
        _validate_database(source)
        source.backup(target)
        target.close()
        target = None
        validation = _read_only_connection(temporary)
        try:
            _validate_database(validation)
        finally:
            validation.close()
        sha256 = _file_sha256(temporary)
        os.replace(temporary, destination)
        return sha256
    finally:
        source.close()
        if target is not None:
            target.close()
        temporary.unlink(missing_ok=True)
