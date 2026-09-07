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


def test_canonical_url_decodes_only_ascii_unreserved_path_escapes():
    assert canonical_url('https://example.com/index%2Ephp/%41%7a%30%2d%5F%7e') == (
        'https://example.com/index.php/Az0-_~')
    assert canonical_url('https://example.com/index%2ephp') == canonical_url('https://example.com/index.php')
    # Reserved/non-ASCII escapes retain their bytes and case; no recursive decoding.
    path = '/%2F/%2f/%3F/%23/%25/%252E/%C3%A9/%ZZ'
    assert canonical_url('https://example.com' + path) == 'https://example.com' + path
    # Path normalization must not decode the host or reinterpret opaque query data.
    assert canonical_url('https://exa%6Dple.com/index%2ephp?token=a%2Fb%252Ec%26d%3De') == (
        'https://exa%6dple.com/index.php?token=a%2Fb%252Ec%26d%3De')


def test_canonical_path_normalization_does_not_bypass_strict_url_safety():
    import pytest
    from climate_monitor.article_candidate_contract import _validate_url
    for url in ('https://example.com/%2e%2e/private', 'https://127.000.0.1/index%2ephp',
                'file:///index%2ephp', 'https://user:pass@example.com/index%2ephp',
                'https://exa%6Dple.com/index.php', 'https://example.com/%2fprivate'):
        with pytest.raises(ValueError):
            _validate_url(canonical_url(url))
    # The strict validator itself still rejects raw encoded-unreserved URLs.
    with pytest.raises(ValueError):
        _validate_url('https://example.com/index%2Ephp')
    _validate_url(canonical_url('https://example.com/index%2Ephp'))
