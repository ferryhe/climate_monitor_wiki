import pytest

from climate_registry.classification import DocumentPolicy, classify_document


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.worldbank.org/", DocumentPolicy("landing_page", False, "root-url")),
        ("https://example.org/report.pdf", DocumentPolicy("report", True, None)),
        (
            "https://www.iais.org/activities-topics/climate-risk/",
            DocumentPolicy("topic_index", False, "topic-index"),
        ),
        (
            "https://example.org/news/topic/parametric-insurance/",
            DocumentPolicy("topic_index", False, "topic-index"),
        ),
        ("https://example.org/topics/", DocumentPolicy("topic_index", False, "topic-index")),
        (
            "https://example.org/topics/climate/a-specific-article",
            DocumentPolicy("article", True, None),
        ),
        ("https://example.org/news/a-specific-article", DocumentPolicy("article", True, None)),
    ],
)
def test_classification_is_deterministic_and_conservative(url, expected):
    assert classify_document(url) == expected
