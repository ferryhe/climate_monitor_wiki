"""Contract tests for scripts/check_tracked_host_paths.py (issue #152)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_tracked_host_paths import (  # noqa: E402
    find_findings,
    is_excluded,
    scan_repository,
)


def test_flags_host_specific_paths():
    for text in (
        "cd /home/deploy/climate-monitor",
        "secrets live under /root/.hermes",
        "volume /var/lib/docker/volumes/project_data/_data",
    ):
        labels = [label for label, _ in find_findings(text)]
        assert labels, text


def test_flags_private_addresses():
    for text in (
        "SITE_HOST=10.4.5.6",
        "bind 172.20.0.5",
        "caddy listens on 192.168.7.9",
        "host ip-10-0-14-88.internal",
    ):
        labels = [label for label, _ in find_findings(text)]
        assert "private address" in labels or "private hostname" in labels, text


def test_ignores_public_urls_and_placeholders():
    for text in (
        "Source: https://www.ifrs.org/content/ifrs/home/issued-standards/x.html",
        "cd /home/<user>/climate-monitor",
        "See https://example.org/root/index.html for details",
        "the 10.0 release notes",
    ):
        assert find_findings(text) == [], text


def test_exclusions_cover_collected_data_and_fixtures():
    for path in (
        "sources/climate-monitor-2026-04-01.md",
        "monitoring/state/websites/issb-41447f34cf55.json",
        "article_metadata/articles-055-108.json",
        "tests/test_climate_registry_capture.py",
        "monitoring/jobs/weekly-climate-monitor-08h/prompts/weekly-monitor-v1.prompt.md",
    ):
        assert is_excluded(path), path
    assert is_excluded("docs/deployment.md") is False
    # The checker holds the patterns it searches for, so it excludes itself;
    # tests/test_no_host_paths.py pins its matching behaviour instead.
    assert is_excluded("scripts/check_tracked_host_paths.py") is True


def test_repository_tracked_files_are_clean():
    scanned, problems = scan_repository()
    assert scanned > 50, "the scan should cover a meaningful number of tracked files"
    assert problems == [], problems
