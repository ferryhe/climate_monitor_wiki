"""Contract tests for scripts/check_tracked_host_paths.py (issue #152).

The fixtures below are assembled from fragments so that this file does not match the
patterns it exercises -- the checker scans itself and every other tracked file, and a
file that hard-codes a sample host path would be a finding in its own right.

The declared scope is token level and intentionally simple (TypeSafe judgment D):
a path is reported when it starts a token, a third-party http(s) URL is exempt as a
whole, and quote/escape semantics are out of scope. The last two tests pin the
declared out-of-scope behaviour so that widening or narrowing it is a deliberate edit.
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


def test_flags_paths_that_start_a_token():
    for text in (
        "cd " + HOME_ALICE + "/app",
        "paths: [" + HOME_ALICE + "/config]",
        "roots: [" + ROOT_HOME + "/.env]",
        "cmd;" + HOME_ALICE + "/bin/run",
        "KEY=" + HOME_ALICE + "/secret",
        "file://" + HOME_ALICE + "/app",
        "FILE://" + HOME_ALICE + "/app",
        'open "file://' + ROOT_HOME + '/.env"',
        "open FILE://" + ROOT_HOME + "/.env",
    ):
        assert labels_in(text), text


def test_the_local_file_exception_does_not_match_a_scheme_suffix():
    # `profile:` ends with the local-file scheme text but is not that scheme, and a
    # compound scheme such as `profile+file:` is one scheme, not the file scheme.
    assert find_findings("profile:" + HOME_ALICE + "/config") == []
    assert find_findings("curl PROFILE:" + HOME_ALICE + "/config") == []
    assert find_findings("profile+file://" + HOME_ALICE + "/config") == []
    assert find_findings("curl mailto+file://" + HOME_ALICE + "/config") == []


def test_token_logic_ignores_relative_references_and_word_suffixes():
    """A hit glued to a longer token is not this host's path."""
    for text in (
        "./" + "home/alice/icon.svg",
        "cat ./assets-" + "x/home/alice/icon.svg",
        'cat "assets' + HOME_ALICE + "/icon.svg" + '"',
        'cat "/opt' + HOME_ALICE + "/icon.svg" + '"',
        "/api/" + "home/alice",
    ):
        assert find_findings(text) == [], text


def test_ignores_placeholders_publisher_urls_and_non_addresses():
    for text in (
        "Source: https://www.ifrs.org/content/ifrs/home/issued-standards/x.html",
        "See https://example.org/" + "root/index.html for details",
        "cd " + "/" + "home/<user>/climate-monitor",
        "the 10.0 release notes",
        "timeout " + "10." + "999.888.777",
        "release " + "10.4." + "5.6rc1",
        "https://" + "10.4.5." + "6.example.org/api",
        "release v" + "10.4." + "5.6",
        "release gzip-" + "10-0-14-88.tar.gz",
        "ssh ip-" + "10-999-888-777.internal",
    ):
        assert find_findings(text) == [], text


def test_declared_out_of_scope_url_punctuation_is_reported():
    """Declared (conservative): a hit glued to a URL token is exempt, but a hit
    separated from it by whitespace or punctuation is reported. The checker does not
    model URL path/query boundaries -- doing so produced contradictory requirements --
    so publisher-style text that separates a path with `;` or `)` is a false positive
    the project accepts, while a URL path that continues without a break is exempt."""
    # Exempt: the hit continues a URL token (path, or a nested URL in a JSON string).
    for text in (
        "See https://example.org/" + "root/index.html for details",
        "Source: https://www.ifrs.org/content/ifrs/home/issued-standards/x.html",
    ):
        assert find_findings(text) == [], text
    # Reported: the URL is a different token, or punctuation separates the hit.
    for text in (
        "curl https://example.org " + HOME_ALICE + "/config",
        "curl " + '"https://example.org/archive;' + HOME_ALICE + '/page"',
        "MARKER=://;DEST=" + HOME_ALICE + "/config",
        "curl 'https://example.org?q=" + HOME_ALICE + "'",
    ):
        assert labels_in(text), text
    # Indented and all-whitespace prefixes must not raise.
    assert labels_in(HOME_ALICE + "/config")
    assert labels_in("  " + HOME_ALICE + "/config")


def test_declared_out_of_scope_quote_and_escape_semantics_are_not_modelled():
    """Declared: quoted and unquoted forms are judged identically (no quote parsing)."""
    # A quoted relative reference is judged by the same token rule as an unquoted one.
    assert labels_in("cat ./assets\\ " + HOME_ALICE + "/icon.svg") == [
        "host home directory"
    ]
    # An operand glued to an option is not a token start, quoted or not.
    assert find_findings("curl -o" + HOME_ALICE + "/config https://example.org") == []
    assert find_findings('curl "-Lo' + HOME_ALICE + '/config"') == []
    assert labels_in('curl "-o ' + HOME_ALICE + '/config" https://example.org'), "a separated operand is a token start"


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
