#!/usr/bin/env python3
"""Fail when tracked files carry host-specific paths or private addresses.

Why: this repository is public, so tracked files describe a deployment through
environment variables, Compose keys and placeholders instead of one host's
concrete values (see `docs/deployment.md` and issue #152).

The patterns are deliberately generic. A real hostname is not listed here:
naming it in this file would republish the value the check exists to keep out.
The check therefore stays independent of any one deployment.

Excluded tracked paths, with reasons:

* `sources/`, `monitoring/state/`, `article_metadata/` — collected research
  artifacts and captured page URLs; publisher links legitimately contain
  `/home/` inside their own path.
* `tests/` — synthetic fixtures, including private addresses used to exercise
  URL-approval logic.
* `monitoring/jobs/*/prompts/` — the pinned weekly prompt template. Its loader
  redacts host paths at runtime and the file is a versioned provenance
  contract, so it is handled separately from this guard.

Matches inside `http(s)` URLs are ignored for every pattern.

Usage: python scripts/check_tracked_host_paths.py
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCANNED_SUFFIXES = (".md", ".py", ".sh", ".json", ".yml", ".yaml")

EXCLUDED_PREFIXES = (
    "sources/",
    "monitoring/state/",
    "article_metadata/",
    "tests/",
)
EXCLUDED_GLOBS = ("monitoring/jobs/*/prompts/*",)

# This checker necessarily contains the patterns it searches for, so it cannot
# scan its own source. Its behaviour is pinned by tests/test_no_host_paths.py.
SELF_EXCLUDED = ("scripts/check_tracked_host_paths.py",)

URL_RE = re.compile(r"https?://\S+")

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("host home directory", re.compile(r"/home/(?!<)[A-Za-z0-9._-]+/")),
    ("root home directory", re.compile(r"/root/")),
    ("docker volume path", re.compile(r"/var/lib/docker/volumes/")),
    (
        "private address",
        re.compile(
            r"(?<![0-9.])"
            r"(?:10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}"
            r"|192\.168\.[0-9]{1,3}\.[0-9]{1,3}"
            r"|172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})"
            r"(?![0-9.])"
        ),
    ),
    ("private hostname", re.compile(r"ip-10(?:-[0-9]+){3}")),
)


def find_findings(text: str) -> list[tuple[str, str]]:
    """Return (label, matched text) for every host-specific hit in `text`."""
    url_spans = [match.span() for match in URL_RE.finditer(text)]
    findings: list[tuple[str, str]] = []
    for label, pattern in PATTERNS:
        for match in pattern.finditer(text):
            if any(start < match.start() < end for start, end in url_spans):
                continue
            findings.append((label, match.group(0)))
    return findings


def is_excluded(path: str) -> bool:
    if path in SELF_EXCLUDED:
        return True
    if path.startswith(EXCLUDED_PREFIXES):
        return True
    return any(fnmatch.fnmatch(path, glob) for glob in EXCLUDED_GLOBS)


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def scan_repository() -> tuple[int, list[tuple[str, int, str, str]]]:
    """Return (scanned file count, findings as path/line/label/text)."""
    scanned = 0
    problems: list[tuple[str, int, str, str]] = []
    for path in tracked_files():
        if not path.endswith(SCANNED_SUFFIXES) or is_excluded(path):
            continue
        scanned += 1
        try:
            lines = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, start=1):
            for label, matched in find_findings(line):
                problems.append((path, number, label, matched))
    return scanned, problems


def main() -> int:
    scanned, problems = scan_repository()
    if problems:
        print(f"Host-specific values found in {len(problems)} tracked line(s):")
        for path, number, label, matched in problems:
            print(f"  {path}:{number}: {label}: {matched}")
        print()
        print("Use an environment variable, a Compose key, or a placeholder instead.")
        return 1
    print(f"No host-specific paths or private addresses in {scanned} tracked file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
