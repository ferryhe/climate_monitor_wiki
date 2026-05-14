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


def test_dedupe_items_normalizes_tracking_urls_and_titles():
    items = [
        _item("Climate risk report", "https://example.com/report?utm_source=x"),
        _item("Climate risk report ", "https://example.com/report"),
        _item("Different report", "https://example.com/other"),
    ]

    kept, notes = dedupe_items(items, seen_urls=set(), seen_titles=set())

    assert [item.title for item in kept] == ["Climate risk report", "Different report"]
    assert any("duplicate" in note for note in notes)


def test_dedupe_items_uses_existing_seen_url_and_title_sets():
    items = [
        _item("Seen URL", "https://example.com/already?mc_cid=123"),
        _item("Seen Title", "https://example.com/new"),
        _item("Fresh Title", "https://example.com/fresh"),
    ]

    kept, notes = dedupe_items(
        items,
        seen_urls={"https://example.com/already"},
        seen_titles={"seen title"},
    )

    assert [item.title for item in kept] == ["Fresh Title"]
    assert any("URL history" in note for note in notes)
    assert any("duplicate title" in note for note in notes)
