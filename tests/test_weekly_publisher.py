from __future__ import annotations

import subprocess
import sys
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ingest_weekly_reports as ingest
from scripts import publish_weekly_reports as publisher
from scripts.sync_source_wiki import sync_source_wiki


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _report(path: Path, day: str, body: str = "Weekly summary.") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        (
            f"# Weekly Climate & Actuarial Monitor\n\n**Report Date:** {day}\n\n"
            f"## Executive Summary\n\n{body}\n"
        ).encode("utf-8")
    )
    return path


def _advance_main(remote: Path, workspace: Path, message: str) -> str:
    clone = workspace / f"advance-{message}"
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.name", "Race")
    _git(clone, "config", "user.email", "race@example.test")
    marker = clone / f"{message}.txt"
    marker.write_text(message + "\n", encoding="utf-8")
    _git(clone, "add", marker.name)
    _git(clone, "commit", "-m", message)
    _git(clone, "push", "origin", "main")
    return _git(remote, "rev-parse", "main")


def _is_candidate_refspec(value: str) -> bool:
    return ":refs/heads/codex/hermes-weekly-candidate-" in value


def _is_rolling_update(args) -> bool:
    suffix = f":refs/heads/{publisher.BRANCH}"
    return args[:2] == ["git", "push"] and any(
        value.endswith(suffix) and not value.startswith(":") for value in args
    )


def _is_rolling_delete(args) -> bool:
    return args[:2] == ["git", "push"] and f":refs/heads/{publisher.BRANCH}" in args


def _candidate_refs(remote: Path) -> list[str]:
    output = _git(
        remote,
        "for-each-ref",
        "--format=%(refname)",
        "refs/heads/codex/hermes-weekly-candidate-*",
    )
    return output.splitlines() if output else []


def _remote_ref_missing(remote: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=remote,
        capture_output=True,
    ).returncode != 0


def _push_rolling_report(
    remote: Path,
    workspace: Path,
    *,
    filename_day: str,
    internal_day: str | None = None,
    body: str = "Rolling content.",
    wiki_body: str | None = None,
) -> None:
    clone = workspace / f"rolling-{filename_day}"
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.name", "Test")
    _git(clone, "config", "user.email", "test@example.test")
    source = clone / "sources" / f"climate-monitor-{filename_day}.md"
    _report(source, internal_day or filename_day, body)
    wiki = clone / "wiki" / f"climate-monitor-{filename_day}.md"
    wiki.write_bytes(
        f"# Climate Monitor - {filename_day}\n\n{wiki_body or body}\n".encode("utf-8")
    )
    index = clone / "wiki" / "index.md"
    index.write_bytes(index.read_bytes() + f"\n{filename_day}\n".encode("utf-8"))
    _git(clone, "add", "sources", "wiki")
    _git(clone, "commit", "-m", "forged rolling report")
    _git(clone, "push", "origin", f"HEAD:refs/heads/{publisher.BRANCH}")


class FakeGhRunner:
    def __init__(self) -> None:
        self.pr_url: str | None = None
        self.create_calls = 0
        self.close_calls = 0
        self.race_on_create = False

    def __call__(self, args, *, cwd: Path, check: bool = True):
        if args[0] != "gh":
            return publisher.run_command(args, cwd=cwd, check=check)
        if args[1:3] == ["pr", "list"]:
            payload = f'{{"url":"{self.pr_url}"}}' if self.pr_url else ""
            return subprocess.CompletedProcess(args, 0, f"[{payload}]" if payload else "[]", "")
        if args[1:3] == ["pr", "create"]:
            self.create_calls += 1
            self.pr_url = "https://example.test/pull/1"
            if self.race_on_create:
                return subprocess.CompletedProcess(args, 1, "", "already exists")
            return subprocess.CompletedProcess(args, 0, self.pr_url + "\n", "")
        if args[1:3] == ["pr", "close"]:
            self.close_calls += 1
            self.pr_url = None
            return subprocess.CompletedProcess(args, 0, "closed\n", "")
        raise AssertionError(args)


@pytest.fixture
def local_remote(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.test")
    _report(seed / "sources" / "climate-monitor-2026-08-03.md", "2026-08-03")
    (seed / "scripts").mkdir()
    shutil.copyfile(
        Path(publisher.__file__).with_name("sync_source_wiki.py"),
        seed / "scripts" / "sync_source_wiki.py",
    )
    sync_source_wiki(
        source_dir=seed / "sources", wiki_dir=seed / "wiki", cadence="weekly"
    )
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(remote)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    production = tmp_path / "production"
    subprocess.run(
        ["git", "clone", str(remote), str(production)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return remote, production


def _publish(production: Path, reports: Path, runner: FakeGhRunner):
    return publisher.publish(
        production_repo=production,
        report_dir=reports,
        today=date(2026, 8, 10),
        runner=runner,
        verifier=lambda _checkout, _runner: None,
    )


def test_ingest_has_no_git_operations_or_git_flags(monkeypatch, tmp_path):
    source = Path(ingest.__file__).read_text(encoding="utf-8")
    assert "import subprocess" not in source
    assert '"--commit"' not in source
    assert '"--push"' not in source
    assert "subprocess." not in source

    repo = tmp_path / "repo"
    repo.mkdir()
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    monkeypatch.setattr(ingest, "REPO_ROOT", repo)
    monkeypatch.setattr(
        ingest,
        "sync_source_wiki",
        lambda **_kwargs: SimpleNamespace(
            latest_date="2026-08-10",
            daily_pages=1,
            source_days=1,
            created_pages=[],
            updated_pages=[],
            missing_days=[],
            warnings=[],
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["ingest_weekly_reports.py", "--report-dir", str(reports)]
    )
    assert ingest.main() == 0
    assert (repo / "sources" / "climate-monitor-2026-08-10.md").exists()


def test_report_validation_missing_non_monday_mismatch_and_empty(tmp_path):
    with pytest.raises(publisher.PublishError, match="directory not found"):
        publisher.discover_reports(tmp_path / "missing", today=date(2026, 8, 10))

    with pytest.raises(publisher.PublishError, match="not Monday"):
        publisher.validate_report(
            _report(tmp_path / "climate-monitor-2026-08-11.md", "2026-08-11")
        )

    with pytest.raises(publisher.PublishError, match="mismatch"):
        publisher.validate_report(
            _report(tmp_path / "climate-monitor-2026-08-10.md", "2026-08-03")
        )

    empty = tmp_path / "climate-monitor-2026-08-17.md"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(publisher.PublishError, match="empty"):
        publisher.validate_report(empty)


def test_existing_main_report_is_noop(local_remote, tmp_path):
    _, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-03.md", "2026-08-03")
    result = _publish(production, reports, FakeGhRunner())
    assert result.status == "no-op"


def test_first_publish_creates_fixed_branch_and_pr_without_touching_production(
    local_remote, tmp_path
):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    before = (_git(production, "rev-parse", "HEAD"), _git(production, "status", "--porcelain"))
    gh = FakeGhRunner()

    result = _publish(production, reports, gh)

    assert result.status == "published"
    assert result.pr_url == "https://example.test/pull/1"
    assert gh.create_calls == 1
    assert _git(remote, "rev-parse", f"refs/heads/{publisher.BRANCH}") == result.published_sha
    assert (_git(production, "rev-parse", "HEAD"), _git(production, "status", "--porcelain")) == before


def test_subsequent_run_consolidates_all_unmerged_reports(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    gh = FakeGhRunner()
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    first = _publish(production, reports, gh)
    _report(reports / "climate-monitor-2026-08-17.md", "2026-08-17")

    second = publisher.publish(
        production_repo=production,
        report_dir=reports,
        today=date(2026, 8, 17),
        runner=gh,
        verifier=lambda _checkout, _runner: None,
    )

    assert second.status == "published"
    changed = _git(
        remote, "diff", "--name-only", "main...codex/hermes-weekly-monitor"
    ).splitlines()
    assert "sources/climate-monitor-2026-08-10.md" in changed
    assert "sources/climate-monitor-2026-08-17.md" in changed
    assert _git(remote, "rev-list", "--count", "main..codex/hermes-weekly-monitor") == "1"
    assert second.published_sha != first.published_sha


def test_same_generated_tree_does_not_rewrite_branch(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    first = _publish(production, reports, gh)
    second = _publish(production, reports, gh)
    assert second.status == "unchanged"
    assert second.published_sha == first.published_sha
    assert _git(remote, "rev-parse", f"refs/heads/{publisher.BRANCH}") == first.published_sha


@pytest.mark.parametrize(
    "unexpected_path",
    ["wiki/debug.md", "sources/unexpected.md", "debug.txt"],
)
def test_verifier_created_unexpected_path_fails_closed(
    local_remote, tmp_path, unexpected_path
):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")

    def malicious_verifier(checkout: Path, _runner):
        target = checkout / unexpected_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("unexpected\n", encoding="utf-8")

    with pytest.raises(publisher.PublishError, match="unexpected|outside"):
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=FakeGhRunner(),
            verifier=malicious_verifier,
        )
    with pytest.raises(subprocess.CalledProcessError):
        _git(remote, "rev-parse", f"refs/heads/{publisher.BRANCH}")


@pytest.mark.parametrize("operation", ["overwrite", "delete"])
def test_main_source_mutation_fails_closed(local_remote, tmp_path, operation):
    _, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")

    def source_mutator(checkout: Path, _runner):
        existing = checkout / "sources" / "climate-monitor-2026-08-03.md"
        if operation == "overwrite":
            existing.write_text("changed\n", encoding="utf-8")
        else:
            existing.unlink()

    with pytest.raises(publisher.PublishError, match="source|deletion"):
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=FakeGhRunner(),
            verifier=source_mutator,
        )


def test_generation_executes_sync_script_from_clone(local_remote, tmp_path):
    remote, production = local_remote
    admin = tmp_path / "admin"
    subprocess.run(["git", "clone", str(remote), str(admin)], check=True, capture_output=True)
    _git(admin, "config", "user.name", "Test")
    _git(admin, "config", "user.email", "test@example.test")
    sync_script = admin / "scripts" / "sync_source_wiki.py"
    text = sync_script.read_text(encoding="utf-8")
    sync_script.write_text(
        text.replace(
            'f"# Climate Monitor - {day}"',
            'f"# {\'CLONE IMPLEMENTATION\' if day == \'2026-08-10\' else \'Climate Monitor\'} - {day}"',
        ),
        encoding="utf-8",
    )
    _git(admin, "add", "scripts/sync_source_wiki.py")
    _git(admin, "commit", "-m", "change clone sync implementation")
    _git(admin, "push", "origin", "main")

    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    result = _publish(production, reports, FakeGhRunner())
    page = _git(
        remote,
        "show",
        f"{publisher.BRANCH}:wiki/climate-monitor-2026-08-10.md",
    )
    assert result.status == "published"
    assert "# CLONE IMPLEMENTATION - 2026-08-10" in page


def test_existing_branch_outside_allowlist_fails_closed(local_remote, tmp_path):
    remote, production = local_remote
    attacker = tmp_path / "attacker"
    subprocess.run(["git", "clone", str(remote), str(attacker)], check=True, capture_output=True)
    _git(attacker, "config", "user.name", "Test")
    _git(attacker, "config", "user.email", "test@example.test")
    (attacker / "README.md").write_text("unexpected\n", encoding="utf-8")
    _git(attacker, "add", "README.md")
    _git(attacker, "commit", "-m", "bad")
    _git(attacker, "push", "origin", f"HEAD:refs/heads/{publisher.BRANCH}")
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")

    with pytest.raises(publisher.PublishError, match="outside weekly-report allowlist"):
        _publish(production, reports, FakeGhRunner())


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("missing", "authoritative report missing"),
        ("different", "differs from authoritative"),
        ("mismatched_date", "Report Date mismatch"),
        ("offcycle", "invalid rolling source"),
    ],
)
def test_semantically_invalid_rolling_branch_fails_even_on_noop(
    local_remote, tmp_path, mode, expected
):
    remote, production = local_remote
    reports = tmp_path / "reports"
    reports.mkdir()
    filename_day = "2026-08-11" if mode == "offcycle" else "2026-08-10"
    internal_day = "2026-08-03" if mode == "mismatched_date" else filename_day
    _push_rolling_report(
        remote,
        tmp_path,
        filename_day=filename_day,
        internal_day=internal_day,
        body="Rolling bytes.",
    )
    if mode != "missing":
        authority_internal = internal_day if mode == "mismatched_date" else filename_day
        authority_body = "Authoritative bytes." if mode == "different" else "Rolling bytes."
        _report(
            reports / f"climate-monitor-{filename_day}.md",
            authority_internal,
            authority_body,
        )

    with pytest.raises(publisher.PublishError, match=expected):
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 17),
            runner=FakeGhRunner(),
            verifier=lambda _checkout, _runner: None,
        )


def test_rolling_wiki_page_requires_matching_validated_source(local_remote, tmp_path):
    remote, production = local_remote
    attacker = tmp_path / "wiki-only"
    subprocess.run(["git", "clone", str(remote), str(attacker)], check=True, capture_output=True)
    _git(attacker, "config", "user.name", "Test")
    _git(attacker, "config", "user.email", "test@example.test")
    wiki = attacker / "wiki" / "climate-monitor-2026-08-10.md"
    wiki.write_bytes(b"# forged wiki-only page\n")
    _git(attacker, "add", "wiki")
    _git(attacker, "commit", "-m", "wiki without source")
    _git(attacker, "push", "origin", f"HEAD:refs/heads/{publisher.BRANCH}")
    reports = tmp_path / "reports"
    reports.mkdir()

    with pytest.raises(publisher.PublishError, match="no matching authoritative or main source"):
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=FakeGhRunner(),
            verifier=lambda _checkout, _runner: None,
        )


def test_source_only_rolling_is_rebuilt_with_corresponding_wiki(local_remote, tmp_path):
    remote, production = local_remote
    attacker = tmp_path / "source-only"
    subprocess.run(["git", "clone", str(remote), str(attacker)], check=True, capture_output=True)
    _git(attacker, "config", "user.name", "Test")
    _git(attacker, "config", "user.email", "test@example.test")
    source = _report(
        attacker / "sources" / "climate-monitor-2026-08-10.md",
        "2026-08-10",
    )
    index = attacker / "wiki" / "index.md"
    index.write_bytes(index.read_bytes() + b"\n2026-08-10\n")
    _git(attacker, "add", "sources", "wiki/index.md")
    _git(attacker, "commit", "-m", "source without wiki")
    _git(attacker, "push", "origin", f"HEAD:refs/heads/{publisher.BRANCH}")
    reports = tmp_path / "reports"
    reports.mkdir()
    shutil.copyfile(source, reports / source.name)

    result = publisher.publish(
        production_repo=production,
        report_dir=reports,
        today=date(2026, 8, 10),
        runner=FakeGhRunner(),
        verifier=lambda _checkout, _runner: None,
    )
    page = _git(
        remote,
        "show",
        f"{publisher.BRANCH}:wiki/climate-monitor-2026-08-10.md",
    )
    assert result.status == "published"
    assert "# Climate Monitor - 2026-08-10" in page


def test_forged_wiki_for_pending_source_is_rebuilt_deterministically(
    local_remote, tmp_path
):
    remote, production = local_remote
    reports = tmp_path / "reports"
    authority = _report(
        reports / "climate-monitor-2026-08-10.md",
        "2026-08-10",
        "Authoritative weekly summary.",
    )
    _push_rolling_report(
        remote,
        tmp_path,
        filename_day="2026-08-10",
        body="Authoritative weekly summary.",
        wiki_body="FORGED WIKI CONTENT",
    )

    result = publisher.publish(
        production_repo=production,
        report_dir=reports,
        today=date(2026, 8, 10),
        runner=FakeGhRunner(),
        verifier=lambda _checkout, _runner: None,
    )
    source_blob = _git(
        remote,
        "show",
        f"{publisher.BRANCH}:sources/{authority.name}",
    )
    wiki_blob = _git(
        remote,
        "show",
        f"{publisher.BRANCH}:wiki/climate-monitor-2026-08-10.md",
    )
    assert result.status == "published"
    assert "Authoritative weekly summary." in source_blob
    assert "Authoritative weekly summary." in wiki_blob
    assert "FORGED WIKI CONTENT" not in wiki_blob


def test_forged_wiki_after_source_reaches_main_cleans_rolling_and_pr(
    local_remote, tmp_path
):
    remote, production = local_remote
    reports = tmp_path / "reports"
    authority = _report(
        reports / "climate-monitor-2026-08-10.md",
        "2026-08-10",
        "Already merged summary.",
    )
    admin = tmp_path / "merged-main"
    subprocess.run(["git", "clone", str(remote), str(admin)], check=True, capture_output=True)
    _git(admin, "config", "user.name", "Test")
    _git(admin, "config", "user.email", "test@example.test")
    shutil.copyfile(authority, admin / "sources" / authority.name)
    subprocess.run(
        [
            sys.executable,
            "scripts/sync_source_wiki.py",
            "--source-dir",
            str(admin / "sources"),
            "--wiki-dir",
            str(admin / "wiki"),
            "--cadence",
            "weekly",
        ],
        cwd=admin,
        check=True,
        capture_output=True,
    )
    _git(admin, "add", "sources", "wiki")
    _git(admin, "commit", "-m", "merge weekly report")
    _git(admin, "push", "origin", "main")

    forger = tmp_path / "forged-after-main"
    subprocess.run(["git", "clone", str(remote), str(forger)], check=True, capture_output=True)
    _git(forger, "config", "user.name", "Test")
    _git(forger, "config", "user.email", "test@example.test")
    (forger / "wiki" / "climate-monitor-2026-08-10.md").write_bytes(
        b"# FORGED AFTER MAIN\n"
    )
    index = forger / "wiki" / "index.md"
    index.write_bytes(index.read_bytes() + b"\nFORGED INDEX\n")
    _git(forger, "add", "wiki")
    _git(forger, "commit", "-m", "forge generated rolling files")
    _git(forger, "push", "origin", f"HEAD:refs/heads/{publisher.BRANCH}")

    gh = FakeGhRunner()
    gh.pr_url = "https://example.test/pull/1"
    result = publisher.publish(
        production_repo=production,
        report_dir=reports,
        today=date(2026, 8, 10),
        runner=gh,
        verifier=lambda _checkout, _runner: None,
    )

    assert result.status == "cleaned"
    assert _remote_ref_missing(remote, publisher.BRANCH)
    assert gh.close_calls == 1
    assert gh.pr_url is None


def test_stale_main_index_is_reconciled_without_new_import(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    reports.mkdir()
    admin = tmp_path / "stale-index-main"
    subprocess.run(["git", "clone", str(remote), str(admin)], check=True, capture_output=True)
    _git(admin, "config", "user.name", "Test")
    _git(admin, "config", "user.email", "test@example.test")
    (admin / "wiki" / "index.md").write_bytes(b"# stale index\n")
    _git(admin, "add", "wiki/index.md")
    _git(admin, "commit", "-m", "make index stale")
    _git(admin, "push", "origin", "main")

    gh = FakeGhRunner()
    result = publisher.publish(
        production_repo=production,
        report_dir=reports,
        today=date(2026, 8, 10),
        runner=gh,
        verifier=lambda _checkout, _runner: None,
    )

    assert result.status == "published"
    assert result.reports == ()
    assert gh.pr_url == "https://example.test/pull/1"
    assert _git(
        remote, "log", "-1", "--format=%s", f"refs/heads/{publisher.BRANCH}"
    ) == "docs: weekly climate monitor update (2026-08-03)"
    assert "# stale index" not in _git(
        remote, "show", f"refs/heads/{publisher.BRANCH}:wiki/index.md"
    )


def test_lease_race_fails_without_overwriting_remote(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    first = _publish(production, reports, gh)
    _report(reports / "climate-monitor-2026-08-17.md", "2026-08-17")
    calls = 0

    def racing_runner(args, *, cwd: Path, check: bool = True):
        nonlocal calls
        if args[:4] == ["git", "ls-remote", "--heads", "origin"]:
            calls += 1
            if calls == 2:
                return subprocess.CompletedProcess(args, 0, "1" * 40 + f"\trefs/heads/{publisher.BRANCH}\n", "")
        return gh(args, cwd=cwd, check=check)

    with pytest.raises(publisher.PublishError, match="rolling branch changed"):
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 17),
            runner=racing_runner,
            verifier=lambda _checkout, _runner: None,
        )
    assert _git(remote, "rev-parse", f"refs/heads/{publisher.BRANCH}") == first.published_sha


def test_candidate_window_main_race_cleans_candidate_before_retry(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    raced = False
    rolling_absent_during_cleanup = False
    stable_at_pr = False

    def racing_runner(args, *, cwd: Path, check: bool = True):
        nonlocal raced, rolling_absent_during_cleanup, stable_at_pr
        candidate_push = args[:2] == ["git", "push"] and any(
            _is_candidate_refspec(value) and not value.startswith(":") for value in args
        )
        candidate_delete = args[:2] == ["git", "push"] and any(
            _is_candidate_refspec(value) and value.startswith(":") for value in args
        )
        if not raced and candidate_push:
            raced = True
            result = gh(args, cwd=cwd, check=check)
            _advance_main(remote, tmp_path, "candidate-window")
            return result
        if candidate_delete:
            result = gh(args, cwd=cwd, check=check)
            rolling_absent_during_cleanup = (
                rolling_absent_during_cleanup
                or _remote_ref_missing(remote, publisher.BRANCH)
            )
            return result
        if args[:3] == ["gh", "pr", "create"]:
            stable_at_pr = _git(remote, "merge-base", "main", publisher.BRANCH) == _git(
                remote, "rev-parse", "main"
            )
        return gh(args, cwd=cwd, check=check)

    result = publisher.publish(
        production_repo=production,
        report_dir=reports,
        today=date(2026, 8, 10),
        runner=racing_runner,
        verifier=lambda _checkout, _runner: None,
    )

    assert result.status == "published"
    assert rolling_absent_during_cleanup
    assert _candidate_refs(remote) == []
    assert gh.create_calls == 1
    assert stable_at_pr
    assert _git(remote, "merge-base", "main", publisher.BRANCH) == _git(
        remote, "rev-parse", "main"
    )


def test_rolling_push_accept_then_client_error_continues_to_pr(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    lost_ack = False

    def lost_ack_runner(args, *, cwd: Path, check: bool = True):
        nonlocal lost_ack
        if not lost_ack and _is_rolling_update(args):
            lost_ack = True
            result = gh(args, cwd=cwd, check=check)
            assert result.returncode == 0
            raise publisher.PublishError("simulated accepted push with lost ACK")
        return gh(args, cwd=cwd, check=check)

    result = publisher.publish(
        production_repo=production,
        report_dir=reports,
        today=date(2026, 8, 10),
        runner=lost_ack_runner,
        verifier=lambda _checkout, _runner: None,
    )

    assert lost_ack
    assert result.status == "published"
    assert gh.create_calls == 1
    assert result.pr_url == "https://example.test/pull/1"
    assert _git(remote, "rev-parse", publisher.BRANCH) == result.published_sha
    assert _candidate_refs(remote) == []


def test_candidate_push_accept_then_nonzero_continues_to_pr(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    lost_ack = False

    def lost_ack_runner(args, *, cwd: Path, check: bool = True):
        nonlocal lost_ack
        candidate_push = args[:2] == ["git", "push"] and any(
            _is_candidate_refspec(value) and not value.startswith(":")
            for value in args
        )
        if not lost_ack and candidate_push:
            lost_ack = True
            result = gh(args, cwd=cwd, check=check)
            assert result.returncode == 0
            return subprocess.CompletedProcess(
                args, 1, "", "simulated candidate push ACK loss"
            )
        return gh(args, cwd=cwd, check=check)

    result = publisher.publish(
        production_repo=production,
        report_dir=reports,
        today=date(2026, 8, 10),
        runner=lost_ack_runner,
        verifier=lambda _checkout, _runner: None,
    )

    assert lost_ack
    assert result.status == "published"
    assert gh.create_calls == 1
    assert result.pr_url == "https://example.test/pull/1"
    assert _git(remote, "rev-parse", publisher.BRANCH) == result.published_sha
    assert _candidate_refs(remote) == []


@pytest.mark.parametrize("remote_accepted", [False, True])
def test_candidate_push_unknown_status_is_critical_and_not_cleaned(
    local_remote, tmp_path, remote_accepted
):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    candidate_branch = None
    published_sha = None
    verify_pending = False
    cleanup_attempted = False

    def unknown_runner(args, *, cwd: Path, check: bool = True):
        nonlocal candidate_branch, published_sha, verify_pending, cleanup_attempted
        candidate_refspec = next(
            (
                value
                for value in args
                if _is_candidate_refspec(value) and not value.startswith(":")
            ),
            None,
        )
        if args[:2] == ["git", "push"] and candidate_refspec:
            published_sha, ref = candidate_refspec.split(":", 1)
            candidate_branch = ref.removeprefix("refs/heads/")
            if remote_accepted:
                result = gh(args, cwd=cwd, check=check)
                assert result.returncode == 0
            verify_pending = True
            raise publisher.PublishError("simulated candidate push transport failure")
        if (
            verify_pending
            and candidate_branch
            and args[:4] == ["git", "ls-remote", "--heads", "origin"]
            and args[-1] == f"refs/heads/{candidate_branch}"
        ):
            verify_pending = False
            raise publisher.PublishError("simulated candidate status query failure")
        if args[:2] == ["git", "push"] and any(
            _is_candidate_refspec(value) and value.startswith(":") for value in args
        ):
            cleanup_attempted = True
        return gh(args, cwd=cwd, check=check)

    with pytest.raises(publisher.PublishError) as caught:
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=unknown_runner,
            verifier=lambda _checkout, _runner: None,
        )

    message = str(caught.value)
    assert "CRITICAL: remote mutation status unknown" in message
    assert f"refs/heads/{candidate_branch}" in message
    assert "expected old state absence" in message
    assert f"candidate/published SHA {published_sha}" in message
    assert "candidate ref may exist and was not cleaned up" in message
    assert "manual intervention required" in message
    assert gh.create_calls == 0
    assert not cleanup_attempted
    if remote_accepted:
        assert _candidate_refs(remote) == [f"refs/heads/{candidate_branch}"]
    else:
        assert _candidate_refs(remote) == []


@pytest.mark.parametrize("remote_accepted", [False, True])
def test_rolling_push_unknown_status_is_critical_without_blind_rollback(
    local_remote, tmp_path, remote_accepted
):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    published_sha = None
    verify_pending = False
    rollback_attempted = False

    def unknown_runner(args, *, cwd: Path, check: bool = True):
        nonlocal published_sha, verify_pending, rollback_attempted
        if _is_rolling_update(args):
            published_sha = args[-1].split(":", 1)[0]
            if remote_accepted:
                result = gh(args, cwd=cwd, check=check)
                assert result.returncode == 0
            verify_pending = True
            raise publisher.PublishError("simulated rolling push transport failure")
        if (
            verify_pending
            and args[:4] == ["git", "ls-remote", "--heads", "origin"]
            and args[-1] == f"refs/heads/{publisher.BRANCH}"
        ):
            verify_pending = False
            raise publisher.PublishError("simulated rolling status query failure")
        if _is_rolling_delete(args):
            rollback_attempted = True
        return gh(args, cwd=cwd, check=check)

    with pytest.raises(publisher.PublishError) as caught:
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=unknown_runner,
            verifier=lambda _checkout, _runner: None,
        )

    message = str(caught.value)
    assert "CRITICAL: remote mutation status unknown" in message
    assert f"refs/heads/{publisher.BRANCH}" in message
    assert "expected old state absence" in message
    assert f"candidate/published SHA {published_sha}" in message
    assert "rolling ref may have been updated and the PR was not operated" in message
    assert "manual intervention required" in message
    assert gh.create_calls == 0
    assert not rollback_attempted
    assert _candidate_refs(remote) == []
    if remote_accepted:
        assert _git(remote, "rev-parse", publisher.BRANCH) == published_sha
    else:
        assert _remote_ref_missing(remote, publisher.BRANCH)


def test_mutation_unknown_primary_is_preserved_when_production_checkout_changes(
    local_remote, tmp_path
):
    _remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    published_sha = None
    verify_pending = False

    def combined_failure_runner(args, *, cwd: Path, check: bool = True):
        nonlocal published_sha, verify_pending
        if _is_rolling_update(args):
            published_sha = args[-1].split(":", 1)[0]
            verify_pending = True
            raise publisher.PublishError("simulated rolling push transport failure")
        if (
            verify_pending
            and args[:4] == ["git", "ls-remote", "--heads", "origin"]
            and args[-1] == f"refs/heads/{publisher.BRANCH}"
        ):
            verify_pending = False
            (production / "unexpected-local-change.txt").write_text(
                "changed during failure handling\n", encoding="utf-8"
            )
            raise publisher.PublishError("simulated rolling status query failure")
        return gh(args, cwd=cwd, check=check)

    with pytest.raises(publisher.PublishError) as caught:
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=combined_failure_runner,
            verifier=lambda _checkout, _runner: None,
        )

    message = str(caught.value)
    assert "CRITICAL: remote mutation status unknown" in message
    assert f"refs/heads/{publisher.BRANCH}" in message
    assert f"candidate/published SHA {published_sha}" in message
    assert "manual intervention required" in message
    assert "checkout-integrity failure" in message
    assert "production checkout changed during publication" in message
    assert isinstance(caught.value.__cause__, publisher.PublishError)
    assert "CRITICAL: remote mutation status unknown" in str(caught.value.__cause__)
    assert gh.create_calls == 0


@pytest.mark.parametrize("failed_command", ["rev-parse", "status"])
def test_ordinary_primary_is_preserved_when_final_integrity_command_fails(
    local_remote, tmp_path, failed_command
):
    _remote, production = local_remote
    reports = tmp_path / "reports"
    reports.mkdir()
    gh = FakeGhRunner()
    matching_calls = 0

    def integrity_failure_runner(args, *, cwd: Path, check: bool = True):
        nonlocal matching_calls
        is_target = (
            Path(cwd).resolve() == production.resolve()
            and args[:2] == ["git", failed_command]
        )
        if is_target:
            matching_calls += 1
            if matching_calls == 2:
                raise publisher.PublishError(
                    f"simulated final {failed_command} integrity failure"
                )
        return gh(args, cwd=cwd, check=check)

    def fail_verification(_checkout, _runner):
        raise publisher.PublishError("ordinary publication failure")

    with pytest.raises(publisher.PublishError) as caught:
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=integrity_failure_runner,
            verifier=fail_verification,
        )

    message = str(caught.value)
    assert "ordinary publication failure" in message
    assert "checkout-integrity failure" in message
    assert f"simulated final {failed_command} integrity failure" in message
    assert isinstance(caught.value.__cause__, publisher.PublishError)
    assert str(caught.value.__cause__) == "ordinary publication failure"


def test_promotion_window_race_rolls_back_before_retry_and_pr(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    raced = False
    rollback_observed = False
    stable_at_pr = False

    def racing_runner(args, *, cwd: Path, check: bool = True):
        nonlocal raced, rollback_observed, stable_at_pr
        if not raced and _is_rolling_update(args):
            raced = True
            result = gh(args, cwd=cwd, check=check)
            _advance_main(remote, tmp_path, "single-promotion-window")
            return result
        if raced and _is_rolling_delete(args):
            result = gh(args, cwd=cwd, check=check)
            rollback_observed = _remote_ref_missing(remote, publisher.BRANCH)
            return result
        if args[:3] == ["gh", "pr", "create"]:
            stable_at_pr = _git(remote, "merge-base", "main", publisher.BRANCH) == _git(
                remote, "rev-parse", "main"
            )
        return gh(args, cwd=cwd, check=check)

    result = publisher.publish(
        production_repo=production,
        report_dir=reports,
        today=date(2026, 8, 10),
        runner=racing_runner,
        verifier=lambda _checkout, _runner: None,
    )

    assert result.status == "published"
    assert rollback_observed
    assert stable_at_pr
    assert gh.create_calls == 1
    assert _candidate_refs(remote) == []


def test_three_promotion_window_races_restore_absent_rolling(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    races = 0

    def racing_runner(args, *, cwd: Path, check: bool = True):
        nonlocal races
        if _is_rolling_update(args):
            races += 1
            result = gh(args, cwd=cwd, check=check)
            _advance_main(remote, tmp_path, f"promotion-race-{races}")
            return result
        return gh(args, cwd=cwd, check=check)

    with pytest.raises(publisher.PublishError, match="all 3 attempts"):
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=racing_runner,
            verifier=lambda _checkout, _runner: None,
        )

    assert races == 3
    assert gh.create_calls == 0
    assert _remote_ref_missing(remote, publisher.BRANCH)
    assert _candidate_refs(remote) == []


def test_three_promotion_races_preserve_existing_open_pr_ref(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    good = _publish(production, reports, gh)
    _report(reports / "climate-monitor-2026-08-17.md", "2026-08-17")
    races = 0

    def racing_runner(args, *, cwd: Path, check: bool = True):
        nonlocal races
        if _is_rolling_update(args) and args[-1].split(":", 1)[0] != good.published_sha:
            races += 1
            result = gh(args, cwd=cwd, check=check)
            _advance_main(remote, tmp_path, f"open-pr-race-{races}")
            return result
        return gh(args, cwd=cwd, check=check)

    with pytest.raises(publisher.PublishError, match="all 3 attempts"):
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 17),
            runner=racing_runner,
            verifier=lambda _checkout, _runner: None,
        )

    assert races == 3
    assert gh.create_calls == 1
    assert gh.pr_url == "https://example.test/pull/1"
    assert _git(remote, "rev-parse", publisher.BRANCH) == good.published_sha
    assert _candidate_refs(remote) == []


def test_rollback_verification_transport_failure_is_critical(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    promoted_sha = None
    rollback_sent = False

    def verification_failure_runner(args, *, cwd: Path, check: bool = True):
        nonlocal promoted_sha, rollback_sent
        if _is_rolling_update(args):
            promoted_sha = args[-1].split(":", 1)[0]
            result = gh(args, cwd=cwd, check=check)
            _advance_main(remote, tmp_path, "rollback-verification-transport")
            return result
        if promoted_sha and _is_rolling_delete(args):
            result = gh(args, cwd=cwd, check=check)
            rollback_sent = True
            return result
        if rollback_sent and args[:4] == ["git", "ls-remote", "--heads", "origin"]:
            rollback_sent = False
            raise publisher.PublishError("simulated rollback verification transport failure")
        return gh(args, cwd=cwd, check=check)

    with pytest.raises(publisher.PublishError) as caught:
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=verification_failure_runner,
            verifier=lambda _checkout, _runner: None,
        )

    message = str(caught.value)
    assert "CRITICAL" in message
    assert f"refs/heads/{publisher.BRANCH}" in message
    assert "rollback status unknown" in message
    assert "expected good state absence" in message
    assert f"candidate/current SHA {promoted_sha}" in message
    assert "manual intervention required" in message
    assert "simulated rollback verification transport failure" in message
    assert gh.create_calls == 0
    assert _candidate_refs(remote) == []


def test_rollback_failure_is_loud_and_stops(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()

    promoted = False

    def rollback_failure_runner(args, *, cwd: Path, check: bool = True):
        nonlocal promoted
        if _is_rolling_update(args):
            promoted = True
            result = gh(args, cwd=cwd, check=check)
            _advance_main(remote, tmp_path, "rollback-failure")
            return result
        if promoted and _is_rolling_delete(args):
            return subprocess.CompletedProcess(
                args, 1, "", "simulated rollback lease failure"
            )
        return gh(args, cwd=cwd, check=check)

    with pytest.raises(
        publisher.PublishError,
        match=r"CRITICAL: rolling ref refs/heads/codex/hermes-weekly-monitor rollback failed",
    ):
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=rollback_failure_runner,
            verifier=lambda _checkout, _runner: None,
        )

    assert gh.create_calls == 0
    assert not _remote_ref_missing(remote, publisher.BRANCH)
    assert _candidate_refs(remote) == []


@pytest.mark.parametrize("existing_rolling", [False, True])
def test_post_promotion_fetch_failure_rolls_back_before_failing(
    local_remote, tmp_path, existing_rolling
):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    good_sha = None
    report_day = date(2026, 8, 10)
    if existing_rolling:
        good_sha = _publish(production, reports, gh).published_sha
        _report(reports / "climate-monitor-2026-08-17.md", "2026-08-17")
        report_day = date(2026, 8, 17)
    promoted = False

    def fetch_failure_runner(args, *, cwd: Path, check: bool = True):
        nonlocal promoted
        if _is_rolling_update(args):
            source_sha = args[-1].split(":", 1)[0]
            if source_sha != good_sha:
                promoted = True
        if promoted and args[:4] == ["git", "fetch", "origin", "main"]:
            promoted = False
            raise publisher.PublishError("simulated post-promotion fetch failure")
        return gh(args, cwd=cwd, check=check)

    with pytest.raises(
        publisher.PublishError,
        match="post-promotion main verification failed after rolling ref was restored",
    ):
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=report_day,
            runner=fetch_failure_runner,
            verifier=lambda _checkout, _runner: None,
        )

    if good_sha:
        assert _git(remote, "rev-parse", publisher.BRANCH) == good_sha
    else:
        assert _remote_ref_missing(remote, publisher.BRANCH)
    assert _candidate_refs(remote) == []


def test_rollback_and_candidate_cleanup_failures_are_aggregated(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    promoted = False

    def double_failure_runner(args, *, cwd: Path, check: bool = True):
        nonlocal promoted
        if _is_rolling_update(args):
            promoted = True
            result = gh(args, cwd=cwd, check=check)
            _advance_main(remote, tmp_path, "double-failure")
            return result
        if promoted and _is_rolling_delete(args):
            return subprocess.CompletedProcess(args, 1, "", "rolling rollback blocked")
        if args[:2] == ["git", "push"] and any(
            _is_candidate_refspec(value) and value.startswith(":") for value in args
        ):
            return subprocess.CompletedProcess(args, 1, "", "candidate cleanup blocked")
        return gh(args, cwd=cwd, check=check)

    with pytest.raises(publisher.PublishError) as caught:
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=double_failure_runner,
            verifier=lambda _checkout, _runner: None,
        )
    message = str(caught.value)
    assert f"refs/heads/{publisher.BRANCH}" in message
    assert "candidate cleanup failed" in message
    assert "refs/heads/codex/hermes-weekly-candidate-" in message
    assert "manual intervention required" in message


def test_candidate_cleanup_failure_preserves_main_change_context(local_remote, tmp_path):
    remote, production = local_remote
    reports = tmp_path / "reports"
    _report(reports / "climate-monitor-2026-08-10.md", "2026-08-10")
    gh = FakeGhRunner()
    raced = False

    def cleanup_failure_runner(args, *, cwd: Path, check: bool = True):
        nonlocal raced
        candidate_push = args[:2] == ["git", "push"] and any(
            _is_candidate_refspec(value) and not value.startswith(":") for value in args
        )
        candidate_delete = args[:2] == ["git", "push"] and any(
            _is_candidate_refspec(value) and value.startswith(":") for value in args
        )
        if not raced and candidate_push:
            raced = True
            result = gh(args, cwd=cwd, check=check)
            _advance_main(remote, tmp_path, "cleanup-context")
            return result
        if candidate_delete:
            return subprocess.CompletedProcess(args, 1, "", "cleanup lease blocked")
        return gh(args, cwd=cwd, check=check)

    with pytest.raises(publisher.PublishError) as caught:
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=cleanup_failure_runner,
            verifier=lambda _checkout, _runner: None,
        )
    message = str(caught.value)
    assert "origin/main changed after candidate push" in message
    assert "candidate cleanup failed" in message
    assert "manual intervention required" in message


def test_stale_candidate_ref_is_reported_before_generation(local_remote, tmp_path):
    remote, production = local_remote
    main_sha = _git(remote, "rev-parse", "main")
    stale = f"{publisher.CANDIDATE_PREFIX}stale"
    _git(remote, "update-ref", f"refs/heads/{stale}", main_sha)
    reports = tmp_path / "reports"
    reports.mkdir()

    with pytest.raises(publisher.PublishError, match="stale candidate refs found"):
        publisher.publish(
            production_repo=production,
            report_dir=reports,
            today=date(2026, 8, 10),
            runner=FakeGhRunner(),
            verifier=lambda _checkout, _runner: None,
        )


def test_pr_creation_race_accepts_pr_created_by_peer(tmp_path):
    runner = FakeGhRunner()
    runner.race_on_create = True
    assert publisher.ensure_pr(runner, tmp_path) == "https://example.test/pull/1"
    assert runner.create_calls == 1


def test_allowlist_rejects_source_overwrite_and_unrelated_wiki():
    with pytest.raises(publisher.PublishError, match="existing source"):
        publisher.validate_allowlist(
            [("M", "sources/climate-monitor-2026-08-10.md")], {"2026-08-10"}
        )
    with pytest.raises(publisher.PublishError, match="unexpected wiki"):
        publisher.validate_allowlist(
            [("M", "wiki/climate-monitor-2026-08-03.md")], {"2026-08-10"}
        )


@pytest.mark.parametrize("status", ["T", "U", "X", "C100"])
def test_allowlist_rejects_unsupported_git_statuses(status):
    with pytest.raises(publisher.PublishError, match="unsupported git change status"):
        publisher.validate_allowlist([(status, "wiki/index.md")])


def test_remote_url_with_embedded_password_is_rejected():
    with pytest.raises(publisher.PublishError, match="credential helper"):
        publisher._validate_remote_url("https://user:secret@example.test/repo.git")
