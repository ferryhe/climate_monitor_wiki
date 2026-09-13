from climate_monitor.models import MonitorSource, SiteScope
from climate_monitor.web_listening_adapter import _seed_urls


def test_host_only_seed_is_explicit_root_before_gateway_binding():
    source = MonitorSource(key="unep", abbreviation="UNEP", full_name="UNEP",
                           url="https://www.unep.org")
    assert _seed_urls(source, None) == ["https://www.unep.org/"]


def test_root_aliases_deduplicate_without_rewriting_article_paths():
    source = MonitorSource(key="unep", abbreviation="UNEP", full_name="UNEP",
                           url="https://www.unep.org")
    scope = SiteScope(source_key="unep", include_patterns=(), exclude_patterns=(), seed_urls=(
        "https://www.unep.org/", "https://www.unep.org/news?q=1",
    ))
    assert _seed_urls(source, scope) == [
        "https://www.unep.org/", "https://www.unep.org/news?q=1",
    ]
