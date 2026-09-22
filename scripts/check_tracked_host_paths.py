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

Address candidates are validated with `ipaddress`, so an impossible octet is not a
finding, and every pattern carries a left boundary, so a relative path or a version
string is not one either.

A match inside the PATH of an http(s) URL is ignored: collected research artifacts and
quoted issue text legitimately contain publisher URLs whose own paths include a home
directory. Nothing else about a URL is exempt -- the authority is scanned like any
other text, and so are the query string, the fragment and whatever follows a shell
separator, because none of those are a path.

This checker is scanned like any other tracked file. Its patterns are assembled from
fragments so the file does not match them, and `tests/test_no_host_paths.py` pins the
assembled behaviour: detection, repository traversal, exit status, and the URL and
boundary cases above.

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
# Paths are matched anywhere; _is_absolute_match below decides whether a hit is this
# host's absolute path (or the operand of a shell option) rather than a relative
# reference or a segment inside a longer path -- token context answers that better
# than a regular expression can.
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


# A URL ends at whitespace, at a delimiter a reader could see, or at a shell separator.
_URL_RE = re.compile(r"\bhttps?://[^\s`)\]>\"'|;&<$\\]+")
_URL_STOPPERS = ("?", "#", ";")

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


def _inside(spans: list[tuple[int, int]], position: int) -> bool:
    return any(start <= position < end for start, end in spans)


def url_path_spans(line: str) -> list[tuple[int, int]]:
    """Return each URL path span on this line: a query, a fragment or a shell tail is not a path."""
    spans: list[tuple[int, int]] = []
    for match in _URL_RE.finditer(line):
        url = match.group(0)
        authority_end = url.find("//") + 2
        path_start = url.find("/", authority_end)
        if path_start == -1:
            continue
        stop = len(url)
        for stopper in _URL_STOPPERS:
            index = url.find(stopper, authority_end)
            if index != -1:
                stop = min(stop, index)
        if stop <= path_start:
            continue  # the URL carries a query or a fragment before any path segment
        spans.append((match.start() + path_start, match.start() + stop))
    return spans


# Delimiters that end a token, so a path starting right after one is a fresh absolute
# path. `;`, `|`, `&` are shell separators, `=`/`:` introduce values, quotes and
# brackets open arguments, and `<`/`$` precede redirects and substitutions.
_DELIMITERS = " \t\"'`=:;,|&<>$(~!#[]"
_URL_PATH_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~%!$&'(*+,;=:@/-"
)


def _quoted_spans(line: str) -> list[tuple[int, int]]:
    """Spans covered by single, double or backtick quoting (backslash escapes count)."""
    spans, index, opener = [], 0, None
    while index < len(line):
        char = line[index]
        if char == "\\" and opener:
            index += 2
            continue
        if opener:
            if char == opener:
                spans.append((opener_index, index + 1))
                opener = None
        elif char in "\"'`":
            opener, opener_index = char, index
        index += 1
    if opener:
        spans.append((opener_index, len(line)))
    return spans


def _is_absolute_match(line: str, start: int) -> bool:
    """True when a path hit is this host's path rather than a relative reference.

    Declared scope: absolute host paths as they appear in tracked documentation and
    scripts. Accepted: start of line; after a token delimiter (whitespace, quotes,
    brackets, `=`, `<`, `;`, ...); after a URL scheme separator such as the one a
    local file URL uses; a quoted operand of a shell option; and the path of a
    quoted http(s) URL path (no query or fragment, URL-legal characters only). Rejected: a segment inside a longer path, a relative
    reference (dot-prefixed, quoted, with an escaped separator, or the multi-slash
    form), and a word suffix. An operand glued to an unquoted shell option is out
    of scope: the deployment documentation forbids host paths outright and that
    form has never appeared in this repository.
    """
    before = line[:start]
    if not before:
        return True
    for quote_start, quote_end in _quoted_spans(line):
        if quote_start < start < quote_end:
            content = line[quote_start + 1 : start]
            if content.startswith((".", "~")):
                return False
            if content.startswith(("http://", "https://")) and not any(
                char.isspace() for char in content
            ):
                # A quoted URL path stays exempt while no query or fragment has begun
                # and every character is legal in a URL (a closing bracket or a
                # backtick therefore ends the URL).
                tail = content[max(content.rfind("http://"), content.rfind("https://")) :]
                return not (
                    "?" not in tail
                    and "#" not in tail
                    and all(char in _URL_PATH_CHARS for char in tail)
                )
            if content.endswith("://"):
                return True
            if not content:
                return True
            if content[-1] in _DELIMITERS:
                return True
            token = content.rsplit(None, 1)[-1]
            return token.startswith("-") and len(token) > 1
    previous = before[-1]
    if previous in _DELIMITERS:
        if previous.isspace():
            index = len(before) - 2
            backslashes = 0
            while index >= 0 and before[index] == chr(92):
                backslashes += 1
                index -= 1
            if backslashes % 2 == 1:
                return False  # an odd run escapes the separator itself
        return True
    return before.endswith("://")


def find_line_findings(line: str) -> list[tuple[str, str]]:
    """Return (label, matched text) for every host-specific value in one line."""
    findings: list[tuple[str, str]] = []
    path_spans = url_path_spans(line)
    for label, pattern in PATH_PATTERNS:
        for match in pattern.finditer(line):
            if _inside(path_spans, match.start()):
                continue
            if not _is_absolute_match(line, match.start()):
                continue
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
