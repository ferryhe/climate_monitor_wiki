#!/usr/bin/env python3
"""Publish Hermes weekly reports through one fixed rolling pull request.

All generated work happens in a temporary clone of the latest ``origin/main``.
The production checkout is used only to discover its remote URL and is never
modified. GitHub authentication is inherited by ``git``/``gh``; credentials are
never accepted as command-line arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate_monitor.dedupe import canonical_url  # noqa: E402
from climate_monitor.run_ledger import (  # noqa: E402
    LedgerError,
    ReportIdentity,
    SCHEMA_VERSION,
    append_attempt,
    build_report_identity,
)
from climate_registry.errors import RegistryBuildError, RegistryInputError  # noqa: E402
from climate_registry.selection import (  # noqa: E402
    SelectionCandidate,
    candidate_payload,
    load_registry_selection_snapshot,
    parse_strict_weekly_report,
    plan_selection,
)

DEFAULT_REPORT_DIR = Path("/home/ubuntu/web_listening/data/reports")
BRANCH = "codex/hermes-weekly-monitor"
BASE_BRANCH = "main"
CANDIDATE_PREFIX = "codex/hermes-weekly-candidate-"
MAX_BASE_ATTEMPTS = 3
REPORT_RE = re.compile(r"^climate-monitor-(\d{4}-\d{2}-\d{2})\.md$")
REPORT_DATE_RE = re.compile(
    r"^\s*\*\*Report Date:\*\*\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE
)

class PublishError(RuntimeError):
    """A fail-closed publishing error."""


class _MainChanged(PublishError):
    """A main check failed during candidate promotion; rebuild and retry."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
Verifier = Callable[[Path, CommandRunner], None]


@dataclass(frozen=True)
class PublishResult:
    status: str
    base_sha: str
    published_sha: str | None = None
    reports: tuple[str, ...] = ()
    pr_url: str | None = None
    report: ReportIdentity | None = None


def run_command(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args), cwd=cwd, text=True, capture_output=True, check=False
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PublishError(f"command failed ({args[0]}): {detail}")
    return result


def _output(runner: CommandRunner, args: Sequence[str], cwd: Path) -> str:
    return runner(args, cwd=cwd).stdout.strip()


def _current_monday(today: date) -> date:
    return today - timedelta(days=today.weekday())


def _validate_remote_url(remote_url: str) -> None:
    parsed = urlsplit(remote_url)
    if parsed.scheme in {"http", "https"} and parsed.username:
        raise PublishError(
            "origin URL contains a credential; use a credential helper instead"
        )


def validate_report(path: Path, *, allow_offcycle: bool = False) -> str:
    match = REPORT_RE.fullmatch(path.name)
    if not match:
        raise PublishError(f"invalid report filename: {path.name}")
    filename_date = date.fromisoformat(match.group(1))
    if filename_date.weekday() != 0 and not allow_offcycle:
        raise PublishError(f"report is not Monday-dated: {path.name}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise PublishError(f"report is empty: {path.name}")
    report_date = REPORT_DATE_RE.search(text)
    if not report_date:
        raise PublishError(f"Report Date is missing: {path.name}")
    if report_date.group(1) != match.group(1):
        raise PublishError(
            f"Report Date mismatch in {path.name}: {report_date.group(1)}"
        )
    return match.group(1)


def discover_reports(
    report_dir: Path, *, today: date, allow_offcycle: bool = False
) -> list[Path]:
    if not report_dir.is_dir():
        raise PublishError(f"report directory not found: {report_dir}")
    cutoff = today if allow_offcycle else _current_monday(today)
    reports: list[Path] = []
    for path in sorted(report_dir.glob("climate-monitor-*.md")):
        match = REPORT_RE.fullmatch(path.name)
        if not match:
            continue
        report_day = date.fromisoformat(match.group(1))
        if report_day > cutoff or (report_day.weekday() != 0 and not allow_offcycle):
            continue
        validate_report(path, allow_offcycle=allow_offcycle)
        reports.append(path)
    return reports


def _final_report_identity(
    checkout: Path, report_dir: Path, *, today: date, allow_offcycle: bool = False
) -> ReportIdentity:
    if allow_offcycle:
        report_date = today.isoformat()
        unavailable_message = "canonical report is unavailable"
    else:
        report_date = _current_monday(today).isoformat()
        unavailable_message = "current Monday report is unavailable"
    filename = f"climate-monitor-{report_date}.md"
    authoritative = report_dir / filename
    final_source = checkout / "sources" / filename
    authoritative_exists = authoritative.is_file()
    final_exists = final_source.is_file()
    if not authoritative_exists and not final_exists:
        raise PublishError(f"{unavailable_message}: {filename}")
    if not authoritative_exists:
        raise PublishError(f"{unavailable_message}: {filename}")
    if not final_exists:
        raise PublishError(f"final canonical report is unavailable: {filename}")
    validate_report(authoritative, allow_offcycle=allow_offcycle)
    validate_report(final_source, allow_offcycle=allow_offcycle)
    authoritative_raw = authoritative.read_bytes()
    final_source_raw = final_source.read_bytes()
    if authoritative_raw != final_source_raw:
        raise PublishError(
            f"authoritative and final canonical report differ: {filename}"
        )
    relative = final_source.relative_to(checkout).as_posix()
    final_result = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=checkout,
        capture_output=True,
        check=False,
    )
    if final_result.returncode:
        raise PublishError(f"final canonical report is not tracked: {filename}")
    tracked_raw = final_result.stdout
    if final_source_raw != tracked_raw:
        raise PublishError(
            f"final canonical report raw bytes differ from HEAD: {filename}"
        )
    try:
        return build_report_identity(
            report_date=report_date,
            filename=filename,
            sha256=hashlib.sha256(final_source_raw).hexdigest(),
            allow_offcycle=allow_offcycle,
        )
    except LedgerError as exc:
        raise PublishError("final canonical report identity is invalid") from exc


def validate_pending_reports(
    reports: Sequence[Path],
    *,
    source_dir: Path,
    registry_database: Path | None = None,
    allow_offcycle: bool = False,
) -> dict[Path, str]:
    """Fail closed on newly introduced report candidates before any copy or push."""

    if not reports:
        return {}
    historical_urls: set[str] = set()
    if registry_database is not None:
        try:
            snapshot = load_registry_selection_snapshot(registry_database, source_dir)
        except (RegistryInputError, RegistryBuildError) as exc:
            raise PublishError(f"registry selection baseline is invalid: {exc}") from exc
        historical_urls.update(snapshot.canonical_urls)

    parsed_reports = []
    for path in reports:
        try:
            report = parse_strict_weekly_report(path, allow_offcycle=allow_offcycle)
        except RegistryInputError as exc:
            raise PublishError(f"pending report is not valid weekly Markdown: {exc}") from exc
        if report.cadence != "weekly" or any(article.pillar not in {"A", "B"} for article in report.articles):
            raise PublishError(f"pending report is not a Pillar A/B weekly report: {path.name}")
        parsed_reports.append(report)

    for report in sorted(parsed_reports, key=lambda item: item.report_date):
        candidates = tuple(
            SelectionCandidate(
                candidate_id=f"item-{index:03d}",
                pillar=article.pillar or "",
                title=article.title,
                summary=article.summary,
                url=article.url,
            )
            for index, article in enumerate(report.articles, 1)
        )
        try:
            plan = plan_selection(
                candidate_payload(report.report_date, candidates),
                historical_urls=historical_urls,
                allow_offcycle=allow_offcycle,
            )
        except RegistryInputError as exc:
            raise PublishError(f"pending report candidate contract is invalid: {report.path.name}") from exc
        rejected = [
            decision
            for decision in plan["decisions"]
            if decision["disposition"] == "rejected"
        ]
        if rejected:
            first = rejected[0]
            raise PublishError(
                "pending report selection rejected "
                f"{report.path.name} {first['candidate_id']}: {first['reason']}"
            )
        historical_urls.update(canonical_url(candidate.url) for candidate in candidates)
    return {report.path: report.sha256 for report in parsed_reports}


def _changed_paths(
    runner: CommandRunner,
    checkout: Path,
    base: str,
    *,
    cached: bool = False,
) -> list[tuple[str, str]]:
    args = ["git", "diff"]
    if cached:
        args.append("--cached")
    args.extend(["--name-status", "-z", "--no-renames", base])
    raw = runner(args, cwd=checkout).stdout
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise PublishError("git diff returned malformed NUL-delimited output")
    return list(zip(fields[0::2], fields[1::2]))


def validate_allowlist(
    changes: list[tuple[str, str]], imported_dates: set[str] | None = None
) -> None:
    expected_sources = (
        {f"sources/climate-monitor-{day}.md" for day in imported_dates}
        if imported_dates is not None
        else None
    )
    for status, path in changes:
        if status.startswith("D") or status.startswith("R"):
            raise PublishError(f"deletion/rename is not allowed: {status} {path}")
        if status not in {"A", "M"}:
            raise PublishError(f"unsupported git change status: {status} {path}")
        source_match = re.fullmatch(r"sources/climate-monitor-(\d{4}-\d{2}-\d{2})\.md", path)
        wiki_match = re.fullmatch(r"wiki/climate-monitor-(\d{4}-\d{2}-\d{2})\.md", path)
        if source_match:
            if status != "A":
                raise PublishError(f"existing source may not be modified: {status} {path}")
            if expected_sources is not None and path not in expected_sources:
                raise PublishError(f"unexpected source change: {path}")
            continue
        if wiki_match:
            if imported_dates is not None and wiki_match.group(1) not in imported_dates:
                raise PublishError(f"unexpected wiki report change: {path}")
            continue
        if path == "wiki/index.md":
            continue
        raise PublishError(f"change outside weekly-report allowlist: {status} {path}")


def validate_remote_branch(
    runner: CommandRunner,
    checkout: Path,
    *,
    ref: str,
    changes: list[tuple[str, str]],
    report_dir: Path,
    allow_offcycle: bool = False,
    today: date,
) -> None:
    added_dates: set[str] = set()
    cutoff = today if allow_offcycle else _current_monday(today)
    for status, path in changes:
        match = re.fullmatch(
            r"sources/climate-monitor-(\d{4}-\d{2}-\d{2})\.md", path
        )
        if not match:
            continue
        report_day = date.fromisoformat(match.group(1))
        if status != "A" or (report_day.weekday() != 0 and not allow_offcycle) or report_day > cutoff:
            raise PublishError(f"invalid rolling source: {status} {path}")
        authoritative = report_dir / Path(path).name
        if not authoritative.is_file():
            raise PublishError(f"authoritative report missing for rolling source: {path}")
        validate_report(authoritative, allow_offcycle=allow_offcycle)
        remote_blob = _output(runner, ["git", "rev-parse", f"{ref}:{path}"], checkout)
        authoritative_blob = _output(
            runner,
            ["git", "hash-object", "--no-filters", str(authoritative.resolve())],
            checkout,
        )
        if remote_blob != authoritative_blob:
            raise PublishError(f"rolling source differs from authoritative report: {path}")
        added_dates.add(match.group(1))

    for _status, path in changes:
        match = re.fullmatch(
            r"wiki/climate-monitor-(\d{4}-\d{2}-\d{2})\.md", path
        )
        if match:
            day = match.group(1)
            main_source = runner(
                [
                    "git",
                    "cat-file",
                    "-e",
                    f"origin/main:sources/climate-monitor-{day}.md",
                ],
                cwd=checkout,
                check=False,
            ).returncode == 0
            if day not in added_dates and not main_source:
                raise PublishError(
                    f"rolling wiki page has no matching authoritative or main source: {path}"
                )


def verify_checkout(checkout: Path, runner: CommandRunner = run_command) -> None:
    runner([sys.executable, "-m", "pytest", "-q"], cwd=checkout)
    runner(["node", "--check", "showcase/app.js"], cwd=checkout)


def _stage_and_validate(
    runner: CommandRunner,
    checkout: Path,
    *,
    base_sha: str,
    imported_dates: set[str],
) -> list[tuple[str, str]]:
    runner(["git", "add", "--", "sources", "wiki"], cwd=checkout)
    unstaged = runner(["git", "diff", "--quiet"], cwd=checkout, check=False)
    if unstaged.returncode:
        raise PublishError("unexpected unstaged tracked changes after verification")
    untracked = runner(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=checkout,
    ).stdout
    if untracked:
        paths = ", ".join(path for path in untracked.split("\0") if path)
        raise PublishError(f"unexpected untracked paths: {paths}")
    changes = _changed_paths(runner, checkout, base_sha, cached=True)
    validate_allowlist(changes, imported_dates)
    runner(["git", "diff", "--cached", "--check"], cwd=checkout)
    return changes


def _remote_branch_sha(runner: CommandRunner, checkout: Path) -> str | None:
    return _remote_ref_sha(runner, checkout, BRANCH)


def _remote_ref_sha(
    runner: CommandRunner, checkout: Path, branch: str
) -> str | None:
    raw = _output(
        runner,
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        checkout,
    )
    return raw.split()[0] if raw else None


def _assert_no_stale_candidates(
    runner: CommandRunner, production_repo: Path
) -> None:
    raw = _output(
        runner,
        [
            "git",
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{CANDIDATE_PREFIX}*",
        ],
        production_repo,
    )
    if raw:
        refs = ", ".join(line.split()[1] for line in raw.splitlines())
        raise PublishError(
            "stale candidate refs found; inspect and remove them manually before "
            f"publishing: {refs}"
        )


def _assert_remotes_unchanged(
    runner: CommandRunner,
    checkout: Path,
    *,
    base_sha: str,
    branch_sha: str | None,
) -> None:
    runner(["git", "fetch", "origin", BASE_BRANCH], cwd=checkout)
    if _output(runner, ["git", "rev-parse", "origin/main"], checkout) != base_sha:
        raise _MainChanged("origin/main changed during publication")
    if _remote_branch_sha(runner, checkout) != branch_sha:
        raise PublishError("rolling branch changed during publication; refusing stale update")


def _fetch_remote_branch(runner: CommandRunner, checkout: Path, ref: str) -> None:
    runner(
        ["git", "fetch", "origin", f"+refs/heads/{BRANCH}:{ref}"], cwd=checkout
    )


def _find_pr(runner: CommandRunner, checkout: Path) -> str | None:
    result = runner(
        [
            "gh", "pr", "list", "--state", "open", "--head", BRANCH,
            "--base", BASE_BRANCH, "--json", "url",
        ],
        cwd=checkout,
    )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PublishError("gh pr list returned invalid JSON") from exc
    return payload[0].get("url") if payload else None


def ensure_pr(runner: CommandRunner, checkout: Path) -> str:
    existing = _find_pr(runner, checkout)
    if existing:
        return existing
    created = runner(
        [
            "gh", "pr", "create", "--title", "Weekly climate monitor update",
            "--body", "Automated weekly report generated by Hermes.",
            "--base", BASE_BRANCH, "--head", BRANCH,
        ],
        cwd=checkout,
        check=False,
    )
    if created.returncode == 0:
        return created.stdout.strip()
    # Another publisher may have created the PR after our first lookup.
    raced = _find_pr(runner, checkout)
    if raced:
        return raced
    raise PublishError(f"gh pr create failed: {(created.stderr or created.stdout).strip()}")


def close_pr_if_open(runner: CommandRunner, checkout: Path) -> str | None:
    existing = _find_pr(runner, checkout)
    if not existing:
        return None
    runner(
        [
            "gh",
            "pr",
            "close",
            existing,
            "--comment",
            "Closing because authoritative weekly generation now matches main.",
        ],
        cwd=checkout,
    )
    return existing


def _push_ref_exact(
    runner: CommandRunner,
    checkout: Path,
    *,
    source_sha: str,
    branch: str,
    expected_sha: str | None,
) -> subprocess.CompletedProcess[str]:
    return runner(
        [
            "git",
            "push",
            f"--force-with-lease=refs/heads/{branch}:{expected_sha or ''}",
            "origin",
            f"{source_sha}:refs/heads/{branch}",
        ],
        cwd=checkout,
        check=False,
    )


def _delete_ref_exact(
    runner: CommandRunner,
    checkout: Path,
    *,
    branch: str,
    expected_sha: str,
    label: str,
) -> None:
    result = runner(
        [
            "git",
            "push",
            f"--force-with-lease=refs/heads/{branch}:{expected_sha}",
            "origin",
            f":refs/heads/{branch}",
        ],
        cwd=checkout,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PublishError(
            f"CRITICAL: {label} refs/heads/{branch} deletion failed; "
            f"manual intervention required: {detail}"
        )


def _rollback_rolling(
    runner: CommandRunner,
    checkout: Path,
    *,
    published_sha: str,
    previous_sha: str | None,
) -> None:
    expected = previous_sha or "absence"
    command_error: str | None = None
    try:
        if previous_sha:
            result = _push_ref_exact(
                runner,
                checkout,
                source_sha=previous_sha,
                branch=BRANCH,
                expected_sha=published_sha,
            )
        else:
            result = runner(
                [
                    "git",
                    "push",
                    f"--force-with-lease=refs/heads/{BRANCH}:{published_sha}",
                    "origin",
                    f":refs/heads/{BRANCH}",
                ],
                cwd=checkout,
                check=False,
            )
        if result.returncode:
            command_error = (result.stderr or result.stdout).strip()
    except Exception as exc:
        command_error = str(exc)
    try:
        current_sha = _remote_branch_sha(runner, checkout)
    except Exception as exc:
        command_context = (
            f"; rollback command also reported: {command_error}"
            if command_error
            else ""
        )
        raise PublishError(
            f"CRITICAL: rolling ref refs/heads/{BRANCH} rollback status unknown; "
            f"expected good state {expected}; candidate/current SHA {published_sha}; "
            f"manual intervention required: rollback verification failed: {exc}"
            f"{command_context}"
        ) from exc
    if current_sha == previous_sha:
        return
    command_context = (
        f"; rollback command reported: {command_error}" if command_error else ""
    )
    if command_error:
        raise PublishError(
            f"CRITICAL: rolling ref refs/heads/{BRANCH} rollback failed for "
            f"expected good state {expected}; candidate/current SHA {published_sha}; "
            f"observed SHA {current_sha or 'absence'}; manual intervention required"
            f"{command_context}"
        )
    raise PublishError(
        f"CRITICAL: rolling ref refs/heads/{BRANCH} rollback status unknown; "
        f"expected good state {expected}; candidate/current SHA {published_sha}; "
        f"observed SHA {current_sha or 'absence'}; manual intervention required"
    )


def _fetched_main_sha(runner: CommandRunner, checkout: Path) -> str:
    runner(["git", "fetch", "origin", BASE_BRANCH], cwd=checkout)
    return _output(runner, ["git", "rev-parse", "origin/main"], checkout)


def _mutation_status_unknown(
    *,
    ref: str,
    expected_old_sha: str | None,
    published_sha: str,
    push_error: str,
    verification_error: Exception,
    candidate_ref: bool,
) -> PublishError:
    expected = expected_old_sha or "absence"
    consequence = (
        "candidate ref may exist and was not cleaned up"
        if candidate_ref
        else "rolling ref may have been updated and the PR was not operated"
    )
    return PublishError(
        f"CRITICAL: remote mutation status unknown for {ref}; expected old state "
        f"{expected}; candidate/published SHA {published_sha}; {consequence}; "
        f"manual intervention required: push reported: {push_error}; remote "
        f"verification failed: {verification_error}"
    )


def _promote_via_candidate(
    runner: CommandRunner,
    checkout: Path,
    *,
    base_sha: str,
    published_sha: str,
    previous_rolling_sha: str | None,
    candidate_branch: str,
) -> None:
    candidate_pushed = False
    primary_error: Exception | None = None
    try:
        candidate_error: str | None = None
        try:
            candidate_result = _push_ref_exact(
                runner,
                checkout,
                source_sha=published_sha,
                branch=candidate_branch,
                expected_sha=None,
            )
            if candidate_result.returncode:
                candidate_error = (
                    candidate_result.stderr or candidate_result.stdout
                ).strip()
        except Exception as exc:
            candidate_error = str(exc)
        if candidate_error is not None:
            try:
                current_candidate = _remote_ref_sha(
                    runner, checkout, candidate_branch
                )
            except Exception as verification_error:
                raise _mutation_status_unknown(
                    ref=f"refs/heads/{candidate_branch}",
                    expected_old_sha=None,
                    published_sha=published_sha,
                    push_error=candidate_error,
                    verification_error=verification_error,
                    candidate_ref=True,
                ) from verification_error
            if current_candidate == published_sha:
                candidate_pushed = True
            elif current_candidate is None:
                raise PublishError(f"candidate ref push failed: {candidate_error}")
            else:
                raise PublishError(
                    f"candidate ref refs/heads/{candidate_branch} changed during creation"
                )
        else:
            candidate_pushed = True

        if _fetched_main_sha(runner, checkout) != base_sha:
            raise _MainChanged("origin/main changed after candidate push")

        rolling_error: Exception | None = None
        try:
            rolling_result = _push_ref_exact(
                runner,
                checkout,
                source_sha=published_sha,
                branch=BRANCH,
                expected_sha=previous_rolling_sha,
            )
        except Exception as exc:
            rolling_error = exc
            rolling_result = None
        if rolling_error is not None or rolling_result.returncode:
            detail = (
                str(rolling_error)
                if rolling_error is not None
                else (rolling_result.stderr or rolling_result.stdout).strip()
            )
            try:
                current_rolling = _remote_branch_sha(runner, checkout)
            except Exception as verification_error:
                raise _mutation_status_unknown(
                    ref=f"refs/heads/{BRANCH}",
                    expected_old_sha=previous_rolling_sha,
                    published_sha=published_sha,
                    push_error=detail,
                    verification_error=verification_error,
                    candidate_ref=False,
                ) from verification_error
            if current_rolling == published_sha:
                # The remote accepted the exact-leased update even though the client
                # lost its acknowledgement. Continue with the post-promotion main
                # check before creating or reusing the PR.
                pass
            elif current_rolling != previous_rolling_sha:
                raise PublishError("rolling branch changed during promotion")
            else:
                if _fetched_main_sha(runner, checkout) != base_sha:
                    raise _MainChanged("origin/main changed before rolling promotion")
                raise PublishError(f"rolling ref promotion failed: {detail}")

        try:
            verified_main = _fetched_main_sha(runner, checkout)
        except Exception as verification_error:
            try:
                _rollback_rolling(
                    runner,
                    checkout,
                    published_sha=published_sha,
                    previous_sha=previous_rolling_sha,
                )
            except Exception as rollback_error:
                raise PublishError(
                    f"{rollback_error}; post-promotion main verification also failed: "
                    f"{verification_error}"
                ) from rollback_error
            raise PublishError(
                "post-promotion main verification failed after rolling ref was "
                f"restored: {verification_error}"
            ) from verification_error

        if verified_main != base_sha:
            _rollback_rolling(
                runner,
                checkout,
                published_sha=published_sha,
                previous_sha=previous_rolling_sha,
            )
            raise _MainChanged("origin/main changed during rolling promotion window")
    except Exception as exc:
        primary_error = exc

    cleanup_error: Exception | None = None
    if candidate_pushed:
        try:
            _delete_ref_exact(
                runner,
                checkout,
                branch=candidate_branch,
                expected_sha=published_sha,
                label="candidate ref",
            )
        except Exception as exc:
            cleanup_error = exc

    if primary_error and cleanup_error:
        raise PublishError(
            f"{primary_error}; additionally candidate cleanup failed for "
            f"refs/heads/{candidate_branch}: {cleanup_error}; manual intervention required"
        ) from primary_error
    if primary_error:
        raise primary_error
    if cleanup_error:
        raise cleanup_error


def _publish_attempt(
    *,
    remote_url: str,
    report_dir: Path,
    today: date,
    runner: CommandRunner,
    verifier: Verifier,
    candidate_branch: str,
    registry_database: Path | None,
    allow_offcycle: bool = False,
) -> PublishResult:
    with tempfile.TemporaryDirectory(prefix="climate-weekly-publish-") as tmp:
        checkout = Path(tmp) / "repo"
        runner(
            ["git", "clone", "--branch", BASE_BRANCH, remote_url, str(checkout)],
            cwd=Path(tmp),
        )
        runner(["git", "fetch", "origin", BASE_BRANCH], cwd=checkout)
        base_sha = _output(runner, ["git", "rev-parse", "origin/main"], checkout)
        runner(["git", "reset", "--hard", base_sha], cwd=checkout)

        remote_branch_sha = _remote_branch_sha(runner, checkout)
        if remote_branch_sha:
            _fetch_remote_branch(runner, checkout, "refs/publisher/existing")
            remote_changes = _changed_paths(
                runner, checkout, "origin/main...refs/publisher/existing"
            )
            validate_allowlist(remote_changes)
            validate_remote_branch(
                runner,
                checkout,
                ref="refs/publisher/existing",
                changes=remote_changes,
                report_dir=report_dir,
                today=today,
                allow_offcycle=allow_offcycle,
            )

        discovered = discover_reports(report_dir, today=today, allow_offcycle=allow_offcycle)
        pending = [
            report
            for report in discovered
            if not (checkout / "sources" / report.name).exists()
        ]
        validated_reports = validate_pending_reports(
            pending,
            source_dir=checkout / "sources",
            registry_database=registry_database,
            allow_offcycle=allow_offcycle,
        )

        imported: list[str] = []
        for report in pending:
            destination = checkout / "sources" / report.name
            shutil.copyfile(report, destination)
            copied_sha = hashlib.sha256(destination.read_bytes()).hexdigest()
            if copied_sha != validated_reports[report]:
                raise PublishError(
                    f"authoritative report changed after selection validation: {report.name}"
                )
            imported.append(validate_report(destination, allow_offcycle=allow_offcycle))

        runner(
            [
                sys.executable,
                "scripts/sync_source_wiki.py",
                "--source-dir",
                str(checkout / "sources"),
                "--wiki-dir",
                str(checkout / "wiki"),
                "--cadence",
                "weekly",
            ],
            cwd=checkout,
        )
        verifier(checkout, runner)
        changes = _stage_and_validate(
            runner,
            checkout,
            base_sha=base_sha,
            imported_dates=set(imported),
        )
        if changes:
            known_dates = [
                validate_report(path, allow_offcycle=allow_offcycle)
                for path in discover_reports(
                    checkout / "sources", today=today, allow_offcycle=allow_offcycle
                )
            ]
            commit_date = max(
                known_dates, default=_current_monday(today).isoformat()
            )
            runner(
                [
                    "git",
                    "-c",
                    "user.name=Hermes climate monitor",
                    "-c",
                    "user.email=hermes-climate-monitor@users.noreply.github.com",
                    "commit",
                    "-m",
                    f"docs: weekly climate monitor update ({commit_date})",
                ],
                cwd=checkout,
            )
            published_sha = _output(runner, ["git", "rev-parse", "HEAD"], checkout)
        else:
            published_sha = base_sha
        published_tree = _output(runner, ["git", "rev-parse", "HEAD^{tree}"], checkout)

        _assert_remotes_unchanged(
            runner,
            checkout,
            base_sha=base_sha,
            branch_sha=remote_branch_sha,
        )
        report_identity = _final_report_identity(
            checkout, report_dir, today=today, allow_offcycle=allow_offcycle
        )
        remote_tree = None
        if remote_branch_sha:
            remote_tree = _output(
                runner,
                ["git", "rev-parse", "refs/publisher/existing^{tree}"],
                checkout,
            )

        if not changes:
            if not remote_branch_sha:
                return PublishResult(
                    status="no-op", base_sha=base_sha, report=report_identity
                )
            _delete_ref_exact(
                runner,
                checkout,
                branch=BRANCH,
                expected_sha=remote_branch_sha,
                label="obsolete rolling ref",
            )
            if _fetched_main_sha(runner, checkout) != base_sha:
                raise _MainChanged("origin/main changed while cleaning rolling ref")
            closed_pr = close_pr_if_open(runner, checkout)
            return PublishResult(
                status="cleaned",
                base_sha=base_sha,
                reports=tuple(imported),
                pr_url=closed_pr,
                report=report_identity,
            )

        if remote_tree == published_tree:
            pr_url = ensure_pr(runner, checkout)
            return PublishResult(
                status="unchanged",
                base_sha=base_sha,
                published_sha=remote_branch_sha,
                reports=tuple(imported),
                pr_url=pr_url,
                report=report_identity,
            )

        _promote_via_candidate(
            runner,
            checkout,
            base_sha=base_sha,
            published_sha=published_sha,
            previous_rolling_sha=remote_branch_sha,
            candidate_branch=candidate_branch,
        )
        pr_url = ensure_pr(runner, checkout)
        return PublishResult(
            status="published",
            base_sha=base_sha,
            published_sha=published_sha,
            reports=tuple(imported),
            pr_url=pr_url,
            report=report_identity,
        )


def publish(
    *,
    production_repo: Path,
    report_dir: Path,
    today: date | None = None,
    runner: CommandRunner = run_command,
    verifier: Verifier = verify_checkout,
    registry_database: Path | None = None,
    allow_offcycle: bool = False,
) -> PublishResult:
    production_repo = production_repo.resolve()
    before_head = _output(runner, ["git", "rev-parse", "HEAD"], production_repo)
    before_status = _output(runner, ["git", "status", "--porcelain"], production_repo)
    remote_url = _output(
        runner, ["git", "remote", "get-url", "origin"], production_repo
    )
    _validate_remote_url(remote_url)
    _assert_no_stale_candidates(runner, production_repo)
    candidate_branch = f"{CANDIDATE_PREFIX}{secrets.token_hex(8)}"
    primary_error: Exception | None = None
    try:
        for attempt in range(1, MAX_BASE_ATTEMPTS + 1):
            try:
                return _publish_attempt(
                    remote_url=remote_url,
                    report_dir=report_dir,
                    today=today or date.today(),
                    runner=runner,
                    verifier=verifier,
                    candidate_branch=candidate_branch,
                    registry_database=registry_database,
                    allow_offcycle=allow_offcycle,
                )
            except _MainChanged as exc:
                if attempt == MAX_BASE_ATTEMPTS:
                    raise PublishError(
                        f"origin/main changed during all {MAX_BASE_ATTEMPTS} attempts"
                    ) from exc
    except Exception as exc:
        primary_error = exc
        raise
    finally:
        integrity_error: Exception | None = None
        try:
            after_head = _output(
                runner, ["git", "rev-parse", "HEAD"], production_repo
            )
            after_status = _output(
                runner, ["git", "status", "--porcelain"], production_repo
            )
            if (after_head, after_status) != (before_head, before_status):
                integrity_error = PublishError(
                    "production checkout changed during publication"
                )
        except Exception as exc:
            integrity_error = exc
        if integrity_error is not None:
            if primary_error is None:
                raise integrity_error
            raise PublishError(
                f"{primary_error}; additionally checkout-integrity failure: "
                f"{integrity_error}"
            ) from primary_error

    raise AssertionError("publication retry loop exited unexpectedly")


def _publisher_attempt(
    *,
    report_date: str,
    status: str,
    result_code: str,
    finished_at: datetime,
    report: ReportIdentity | None = None,
) -> dict[str, object]:
    scheduled = datetime.fromisoformat(f"{report_date}T10:00:00+00:00")
    if finished_at < scheduled:
        scheduled = datetime.fromisoformat(f"{report_date}T00:00:00+00:00")
    attempt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": (
            f"{finished_at:%Y%m%dt%H%M%Sz}-publisher-{secrets.token_hex(4)}"
        ),
        "stage": "publisher",
        "report_date": report_date,
        "scheduled_for": scheduled.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "result_code": result_code,
    }
    if report is not None:
        attempt["report"] = report.as_record()
    return attempt


def _append_publisher_result(
    ledger_dir: Path,
    *,
    result: PublishResult | None,
    report_date: str,
    repository_root: Path,
    allow_offcycle: bool = False,
) -> str:
    finished_at = datetime.now(timezone.utc).replace(microsecond=0)
    if result is None:
        attempt = _publisher_attempt(
            report_date=report_date,
            status="failed",
            result_code="publish_failed",
            finished_at=finished_at,
        )
    else:
        if result.report is None:
            raise PublishError("publisher result is missing canonical report identity")
        ledger_status = "success" if result.status == "published" else "no_change"
        attempt = _publisher_attempt(
            report_date=result.report.report_date,
            status=ledger_status,
            result_code=result.status,
            finished_at=finished_at,
            report=result.report,
        )
    try:
        return append_attempt(
            ledger_dir,
            attempt,
            repository_root=repository_root,
            allow_offcycle=allow_offcycle,
        )
    except LedgerError as exc:
        raise PublishError("publisher ledger append failed") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--registry-database", type=Path)
    parser.add_argument("--ledger-dir", type=Path, required=True)
    parser.add_argument(
        "--allow-offcycle",
        action="store_true",
        help="Allow non-Monday report dates (manual re-runs). Off by default.",
    )
    args = parser.parse_args()
    run_today = date.today()
    report_date = (
        run_today.isoformat()
        if args.allow_offcycle
        else _current_monday(run_today).isoformat()
    )
    try:
        result = publish(
            production_repo=args.production_repo,
            report_dir=args.report_dir,
            today=run_today,
            registry_database=args.registry_database,
            allow_offcycle=args.allow_offcycle,
        )
    except Exception as exc:
        try:
            _append_publisher_result(
                args.ledger_dir,
                result=None,
                report_date=report_date,
                repository_root=args.production_repo.resolve(),
                allow_offcycle=args.allow_offcycle,
            )
        except PublishError as ledger_error:
            raise PublishError(f"{exc}; {ledger_error}") from exc
        raise
    try:
        ledger_status = _append_publisher_result(
            args.ledger_dir,
            result=result,
            report_date=report_date,
            repository_root=args.production_repo.resolve(),
            allow_offcycle=args.allow_offcycle,
        )
    except PublishError as exc:
        try:
            _append_publisher_result(
                args.ledger_dir,
                result=None,
                report_date=report_date,
                repository_root=args.production_repo.resolve(),
                allow_offcycle=args.allow_offcycle,
            )
        except PublishError as failure_error:
            raise PublishError(f"{exc}; {failure_error}") from exc
        raise
    print(
        f"publisher: status={result.status} base={result.base_sha} "
        f"published={result.published_sha or '-'} reports={len(result.reports)} "
        f"pr={result.pr_url or '-'} ledger={ledger_status}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublishError as exc:
        print(f"publisher: ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
