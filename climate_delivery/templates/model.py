"""Immutable display values only. No source/query/delivery objects cross here."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Citation:
    label: str
    url: str | None = None


@dataclass(frozen=True)
class Update:
    title: str
    institution: str
    topic: str
    article_id: str | None
    content_version: str | None
    publication_date: str | None
    coverage_period: tuple[str, str] | None
    paragraphs: tuple[str, ...]
    metadata: tuple[tuple[str, str], ...]
    citations: tuple[Citation, ...]
    content_sha256: str | None = None
    article_id_label: str = "Article ID"
    imported_from_pdf: bool = False
    date_basis: str | None = None
    information_date: str | None = None
    collected_at: str | None = None


@dataclass(frozen=True)
class Report:
    kind: str
    input_id: str
    input_sha256: str
    title: str
    edition: str
    window: str
    run_date: str | None
    executive_summary: tuple[str, ...]
    summary_citations: tuple[Citation, ...]
    updates: tuple[Update, ...]
    key_dates: tuple[tuple[str, str, str, str, str], ...] = ()
    date_notes: tuple[str, ...] = ()
    statistics: tuple[tuple[str, str], ...] = ()
    coverage_notes: tuple[str, ...] = ()
    coverage: tuple[tuple[str, str, str], ...] = ()
    route_corrections: tuple[tuple[str, str], ...] = ()
    glossary: tuple[tuple[str, str], ...] = ()
    cross_cutting_watch: tuple[str, ...] = ()
