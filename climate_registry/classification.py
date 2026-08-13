from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class DocumentPolicy:
    document_kind: str
    publication_eligible: bool
    exclusion_reason: str | None


def classify_document(url: str) -> DocumentPolicy:
    """Classify a canonical URL using deterministic, URL-only rules."""

    path = urlparse(url).path.rstrip("/")
    if not path:
        return DocumentPolicy("landing_page", False, "root-url")
    if path.casefold().endswith(".pdf"):
        return DocumentPolicy("report", True, None)

    segments = [segment.casefold() for segment in path.split("/") if segment]
    topic_markers = {"topic", "topics", "activities-topics"}
    if segments[-1] in topic_markers or (len(segments) >= 2 and segments[-2] in topic_markers):
        return DocumentPolicy("topic_index", False, "topic-index")
    return DocumentPolicy("article", True, None)
