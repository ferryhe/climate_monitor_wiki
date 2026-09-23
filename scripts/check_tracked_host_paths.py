#!/usr/bin/env python3
"""Fail when tracked files carry one host's concrete deployment values.

Tracked files describe a deployment through environment variables, Compose keys and
placeholders -- never through the concrete values of one machine (see
`docs/deployment.md` and issue #152). This repository is public, so a leak here is a
leak everywhere.

The patterns are generic. This host's real DNS name is deliberately absent: naming it
in this file would republish the value the check exists to keep out.

  * a host home directory, with or without a trailing slash
  * the root home directory
  * a Docker volume path below the Docker root
  * a per-user runtime directory
  * a private address in the RFC 1918 ranges
  * an EC2-style private hostname

# Declared scope: token level, deliberately simple

A path is reported only when it starts a token -- it sits at the start of the line
content, or right after a character that cannot continue a token. A hit glued to a
longer token is not this host's path: an option operand (`-o` + path), a segment
inside a relative path, a word suffix and a version string are all ignored for the
same reason, and so is a hit that continues a http(s) URL token, because a URL path or
query may legally contain any punctuation. A hit that is separated from the URL by
whitespace or punctuation, or that follows one, is reported: the checker does not model
URL path boundaries, which is a false positive the project accepts rather than a
contradictory rule. A local file URL is not exempt, because the path it carries is a
real host path.

Address candidates are validated with `ipaddress`, so an impossible octet is not a
finding, and every address pattern carries a right boundary, so a longer hostname or
a version suffix is not one either.

Quoting and escape semantics are deliberately NOT modelled. A quoted value is judged
by the same token rule as an unquoted one, so an escaped separator (an escaped space)
and a quoted relative reference behave exactly like their unquoted forms. That keeps
the check small and free of the boundary cases a quote-aware parser would have to
chase; those classes are declared out of scope rather than special-cased.

This checker is scanned like any other tracked file. Its patterns are assembled from
fragments so the file does not match them, and `tests/test_no_host_paths.py` pins the
assembled behaviour: detection, repository traversal, exit status and the boundary
cases above.

Exemptions are exact paths with a stated reason and, where the file is pinned, its
SHA-256 -- there are no directory exemptions. The only entry today is the weekly
monitor prompt template: a versioned provenance artifact whose bytes are fixed by the
pipeline contract (the prompt loader verifies this SHA-256 and returns the bytes
unchanged). Editing the file lapses the exemption automatically, so the guard starts
failing until the template is migrated deliberately -- see #154.

Usage: python scripts/check_tracked_host_paths.py
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Assembled from fragments on purpose: this file is scanned too, so it must not match
# the patterns it defines (see the module docstring).
_HOME_DIR = re.compile(r"/" + "home/(?!<)[A-Za-z0-9._-]+")
_ROOT_DIR = re.compile(r"/" + "root(?![A-Za-z0-9._-])")
_VOLUME_DIR = re.compile(r"/" + "var/lib/" + "docker/volumes/")
_RUNTIME_DIR = re.compile(r"/" + "run/user/[0-9]+")
_PRIVATE_ADDRESS = re.compile(r"(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?!\.?[-0-9A-Za-z_])")
_PRIVATE_HOSTNAME = re.compile(r"(?<![\w.-])ip-([0-9]{1,3}(?:-[0-9]{1,3}){3})(?![\w-])")

_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(".".join(str(octet) for octet in octets) + f"/{prefix}")
    for octets, prefix in (((10, 0, 0, 0), 8), ((172, 16, 0, 0), 12), ((192, 168, 0, 0), 16))
)

PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("host home directory", _HOME_DIR),
    ("root home directory", _ROOT_DIR),
    ("docker volume path", _VOLUME_DIR),
    ("host runtime path", _RUNTIME_DIR),
)

# Characters that continue a token: a path hit directly after one of these is part of
# a longer token (an option operand, a relative path, a URL segment, a word suffix, a
# scheme-prefixed value). The local file scheme is handled explicitly below.
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-/:+")
_FILE_SCHEME = "file://"
_FILE_SCHEME_PREFIX = re.compile(r"(?<![A-Za-z0-9+.-])file:$", re.IGNORECASE)

_PROMPT_ARTIFACT = (
    "monitoring/jobs/weekly-climate-monitor-08h/prompts/" + "weekly-monitor-v1.prompt.md"
)
_PROMPT_ARTIFACT_SHA256 = (
    "53896231729799845b48e85eb5901cacce03aa26cec2199e2176dc254d5b7909"
)
EXEMPT_PATHS: dict[str, tuple[str, str]] = {
    _PROMPT_ARTIFACT: (
        "hash-pinned provenance artifact: the prompt loader verifies this SHA-256 and "
        "returns the bytes unchanged; migrating it is #154",
        _PROMPT_ARTIFACT_SHA256,
    ),
}


def _is_absolute_match(line: str, start: int) -> bool:
    """True when a path hit starts a token rather than continuing a longer one.

    See the module docstring for the declared scope. Quoted and unquoted forms are
    judged identically on purpose: quote and escape semantics are out of scope.
    """
    before = line[:start]
    if not before or before[-1] not in _TOKEN_CHARS:
        return True
    index = len(before)
    while index and before[index - 1] in _TOKEN_CHARS:
        index -= 1
    prefix = before[:index]
    # A local file URL carries this host's path even though it is glued to a token.
    # The scheme is matched case-insensitively and must start a scheme, so that a word
    # merely ending in it (for example `profile:`) is not mistaken for one.
    return bool(_FILE_SCHEME_PREFIX.search(prefix)) or before[index:].lower().startswith(_FILE_SCHEME)


def find_line_findings(line: str) -> list[tuple[str, str]]:
    """Return (label, matched text) for every host-specific value in one line."""
    findings: list[tuple[str, str]] = []
    for label, pattern in PATH_PATTERNS:
        for match in pattern.finditer(line):
            if _is_absolute_match(line, match.start()):
                findings.append((label, match.group(0)))
    for match in _PRIVATE_ADDRESS.finditer(line):
        try:
            address = ipaddress.ip_address(match.group(0))
        except ValueError:
            continue  # not an address at all, for example a version string
        if any(address in network for network in _PRIVATE_NETWORKS):
            findings.append(("private address", match.group(0)))
    for match in _PRIVATE_HOSTNAME.finditer(line):
        try:
            dash_form = ipaddress.ip_address(".".join(match.group(1).split("-")))
        except ValueError:
            continue  # impossible octets, for example ip-10-999-888-777
        if any(dash_form in network for network in _PRIVATE_NETWORKS):
            findings.append(("private hostname", match.group(0)))
    return findings


def find_findings(text: str) -> list[tuple[int, str, str]]:
    """Return (line number, label, matched text) for every host-specific value."""
    return [
        (number, label, matched)
        for number, line in enumerate(text.splitlines(), start=1)
        for label, matched in find_line_findings(line)
    ]


def tracked_files(root: Path | None = None) -> list[str]:
    root = root or REPO_ROOT
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def exemption_reason(path: str, raw: bytes) -> str | None:
    """Return the reason this tracked file is exempt, or None when it is scanned."""
    entry = EXEMPT_PATHS.get(path)
    if entry is None:
        return None
    reason, pinned_sha256 = entry
    if hashlib.sha256(raw).hexdigest() != pinned_sha256:
        return None  # edited since the exemption was granted, so scan it
    return reason


def scan_paths(
    relative_paths: Iterable[str], root: Path | None = None
) -> tuple[int, list[tuple[str, int, str, str]]]:
    """Scan the named files under root: (scanned count, path/line/label/text findings)."""
    root = root or REPO_ROOT
    scanned = 0
    problems: list[tuple[str, int, str, str]] = []
    for path in relative_paths:
        raw = (root / path).read_bytes()
        if b"\0" in raw:  # binary asset
            continue
        if exemption_reason(path, raw) is not None:
            continue
        scanned += 1
        text = raw.decode("utf-8", errors="replace")
        for number, label, matched in find_findings(text):
            problems.append((path, number, label, matched))
    return scanned, problems


def scan_repository(root: Path | None = None) -> tuple[int, list[tuple[str, int, str, str]]]:
    """Return (scanned file count, findings as path/line/label/text)."""
    root = root or REPO_ROOT
    return scan_paths(tracked_files(root), root)


def main() -> int:
    scanned, problems = scan_repository()
    if problems:
        print(f"Host-specific values found in {len(problems)} tracked line(s):")
        for path, number, label, matched in problems:
            print(f"  {path}:{number}: {label}: {matched}")
        print()
        print("Use an environment variable, a Compose key, or a placeholder instead.")
        return 1
    print(f"No host-specific values in {scanned} tracked file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
