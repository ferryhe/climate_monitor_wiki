"""Contract tests for scripts/check_tracked_host_paths.py (issue #152).

The fixtures below are assembled from fragments so that this file does not match the
patterns it exercises -- the checker scans itself and every other tracked file, and a
file that hard-codes a sample host path would be a finding in its own right.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.check_tracked_host_paths as checker  # noqa: E402
from scripts.check_tracked_host_paths import (  # noqa: E402
    EXEMPT_PATHS,
    exemption_reason,
    find_findings,
    scan_paths,
    scan_repository,
)

HOME_ALICE = "/" + "home/alice"
ROOT_HOME = "/" + "root"
VOLUME_PATH = "/" + "var/lib/" + "docker/volumes/project_data/_data"
RUNTIME_DIR = "/" + "run/user/" + "1000/" + "climate-hermes-relay"


def labels_in(text: str) -> list[str]:
    return [label for _, label, _ in find_findings(text)]


def test_flags_host_home_directories_with_and_without_trailing_slash():
    for text in (
        f"cd {HOME_ALICE}",
        f"cd {HOME_ALICE}/app",
        f"HOME={ROOT_HOME}",
        f"secrets live under {ROOT_HOME}/.hermes",
    ):
        assert "host home directory" in labels_in(text) or "root home directory" in labels_in(text), text


def test_flags_docker_volume_and_runtime_paths():
    assert "docker volume path" in labels_in(f"volume {VOLUME_PATH}")
    assert "host runtime path" in labels_in(f"relay {RUNTIME_DIR}")


def test_flags_private_addresses_and_hostnames():
    for text in (
        "SITE_HOST=" + "10.4.5." + "6",
        "bind " + "172.20.0." + "5",
        "caddy listens on " + "192.168.7." + "9",
        "host " + "ip-10-" + "0-14-88.internal",
        "ssh ip-" + "172-31-10-77.eu-west-1.compute.internal",
        "ssh ip-" + "192-168-7-9.internal",
        "The server is " + "10.4.5." + "6.",
    ):
        assert labels_in(text), text


def test_flags_private_hosts_in_url_authorities():
    for text in (
        "curl http://" + "10.4.5." + "6/health",
        "https://" + "ip-10-" + "0-14-88.internal/api",
    ):
        assert labels_in(text), text


def test_flags_host_patterns_outside_url_paths():
    # Only a URL *path* is exempt. A closing parenthesis or backtick ends the URL, and a
    # query string, a fragment or a shell separator is not a path either.
    for text in (
        "see `https://example.org)," + HOME_ALICE + "/secret` for details",
        "curl 'https://example.org?q=" + HOME_ALICE + "'",
        "open https://example.org#" + ROOT_HOME + "/.env",
        "curl https://example.org;" + HOME_ALICE + "/bin/run",
        "curl https://example.org<" + HOME_ALICE + "/input",
        'curl "-Lo' + HOME_ALICE + '/config"',
        "file://" + HOME_ALICE + "/app",
        "curl https://example.org/path$(/" + "home/alice/bin/token)",
    ):
        assert labels_in(text), text


def test_ignores_publisher_urls_placeholders_and_non_addresses():
    for text in (
        "Source: https://www.ifrs.org/content/ifrs/home/issued-standards/x.html",
        "See https://example.org/" + "root/index.html for details",
        "cd " + "/" + "home/<user>/climate-monitor",
        "the 10.0 release notes",
        "timeout " + "10." + "999.888.777",
        # Left boundary: a relative path segment or a suffixed version string is not a host.
        "./" + "home/alice/icon.svg",
        "release " + "10.4." + "5.6rc1",
        "https://" + "10.4.5." + "6.example.org/api",
        "release v" + "10.4." + "5.6",
        "release gzip-" + "10-0-14-88.tar.gz",
        "ssh ip-" + "10-999-888-777.internal",
    ):
        assert find_findings(text) == [], text


def test_only_the_pinned_prompt_artifact_is_exempt():
    assert list(EXEMPT_PATHS) == [
        "monitoring/jobs/weekly-climate-monitor-08h/prompts/weekly-monitor-v1.prompt.md"
    ]
    artifact = next(iter(EXEMPT_PATHS))
    raw = (ROOT / artifact).read_bytes()
    assert exemption_reason(artifact, raw) is not None
    # Editing the artifact lapses the exemption instead of hiding the new content.
    assert exemption_reason(artifact, raw + b"\n# edited\n") is None
    # The checker is scanned like any other file: an added host value is a finding.
    assert exemption_reason("scripts/check_tracked_host_paths.py", b"") is None
    assert labels_in("HOST=" + "10.21.32." + "43")


def test_traversal_reports_injected_content_and_the_command_fails(tmp_path, monkeypatch):
    """Detection must hold through tracked-file discovery and the real entry point."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "hostile.md").write_text("HOST=" + "10.21.32." + "43\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
    scanned, problems = checker.scan_repository()
    assert scanned == 1
    assert problems and problems[0][0] == "hostile.md"
    assert checker.main() == 1
    # An empty traversal is reported as zero files, so a silently disabled scan cannot
    # masquerade as a clean repository.
    assert scan_paths([], root=tmp_path)[0] == 0


def test_repository_tracked_files_are_clean():
    scanned, problems = scan_repository()
    assert scanned > 50, "the scan should cover a meaningful number of tracked files"
    assert problems == [], problems


def test_option_operand_without_quoting_is_out_of_scope():
    # Declared scope (TypeSafe judgment C): an unquoted operand glued to a short
    # option is not detected. The conventions in docs/deployment.md forbid host
    # paths outright, and this form has never appeared in the repository.
    assert labels_in("curl -o" + HOME_ALICE + "/config https://example.org") == []


def test_exempts_quoted_url_paths_that_contain_separator_punctuation():
    # A semicolon inside a quoted URL belongs to its path, not to the shell.
    assert labels_in('curl "https://example.org/archive;' + HOME_ALICE + '/page"') == []


def test_boundary_cases_from_review_round_seven():
    # An escaped whitespace does not start a new token; a quoted ordinary path or a
    # path segment inside a longer absolute path is not a host path; a bracketed
    # list is a token boundary; a local file URL inside quotes is a host path.
    assert labels_in("cat ./assets\\ " + HOME_ALICE + "/icon.svg") == []
    assert labels_in('cat "assets' + HOME_ALICE + "/icon.svg" + '"') == []
    assert labels_in('cat "/opt' + HOME_ALICE + "/icon.svg" + '"') == []
    for text in (
        "paths: [" + HOME_ALICE + "/config]",
        "roots: [" + ROOT_HOME + "/.env]",
        'open "file://' + HOME_ALICE + '/config"',
        'open "file://' + ROOT_HOME + '/.env"',
    ):
        assert labels_in(text), text
