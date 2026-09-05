from dataclasses import dataclass

from climate_monitor.dedupe import canonical_url, dedupe_items


@dataclass(frozen=True)
class CandidateItem:
    title: str
    url: str
    summary: str = "summary"
    source_name: str = "Example"
    lane: str = "research"
    climate_related: bool = True
    actuarial_related: bool = True


def _item(title: str, url: str) -> CandidateItem:
    return CandidateItem(title=title, url=url)


def test_canonical_url_removes_tracking_query_params():
    assert canonical_url("HTTPS://Example.com/report/?utm_source=x&gclid=abc&topic=climate") == (
        "https://example.com/report?topic=climate"
    )


def test_dedupe_items_normalizes_tracking_urls_without_using_titles_as_identity():
    items = [
        _item("Climate risk report", "https://example.com/report?utm_source=x"),
        _item("Climate risk report ", "https://example.com/report"),
        _item("Climate risk report", "https://example.com/other"),
    ]

    kept, notes = dedupe_items(items, seen_urls=set())

    assert [item.url for item in kept] == [
        "https://example.com/report?utm_source=x",
        "https://example.com/other",
    ]
    assert len(notes) == 1
    assert "URL history" in notes[0]


def test_dedupe_items_uses_only_existing_seen_urls():
    items = [
        _item("Seen URL", "https://example.com/already?mc_cid=123"),
        _item("Seen Title", "https://example.com/new"),
    ]

    kept, notes = dedupe_items(
        items,
        seen_urls={"https://example.com/already"},
    )

    assert [item.title for item in kept] == ["Seen Title"]
    assert any("URL history" in note for note in notes)
    assert not any("title" in note.casefold() for note in notes)


def test_dedupe_items_keeps_semantic_query_differences():
    items = [
        _item("Edition", "https://example.com/report?edition=2025"),
        _item("Edition", "https://example.com/report?edition=2026"),
    ]

    kept, notes = dedupe_items(items, seen_urls=set())

    assert [item.url for item in kept] == [item.url for item in items]
    assert notes == []
