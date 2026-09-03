from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from climate_registry.audit import build_audit_registry
from climate_registry.errors import RegistryBuildError, RegistryInputError
from climate_registry.reports import parse_historical_report, parse_report_directory
from climate_registry.schema import apply_migrations
from climate_registry.selection import (
    _validate_public_http_url,
    load_selection_input,
    plan_selection,
    plan_registry_selection,
)


def _item(title: str, summary: str, url: str) -> str:
    return f"- **{title}** (web)\n  - {summary}\n  🔗 {url}\n"


def _weekly(day: str, a: str, b: str = "") -> str:
    links = "\n".join(
        f"- {url}"
        for url in ("https://example.com/a", "https://example.com/b")
    )
    return f"""# Weekly Climate & Actuarial Monitor
**Report Date:** {day}
## Executive Summary
- Sites checked: **2**, succeeded: **2**, failed: **0**
## Pillar A — Changes
{a}
## Pillar B — Intelligence
{b}
## Original Links
{links}
"""


def _write_report(source_dir: Path, day: str, a: str, b: str = "") -> Path:
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"climate-monitor-{day}.md"
    path.write_text(_weekly(day, a, b), encoding="utf-8")
    return path


def _registry(tmp_path: Path, source_dir: Path) -> Path:
    database = tmp_path / "registry.sqlite3"
    build_audit_registry(source_dir, database, tmp_path / "audit")
    return database


def _payload(*candidates: dict[str, str], day: str = "2026-08-10") -> dict:
    return {
        "schema_version": "registry-selection-input.v1",
        "report_date": day,
        "candidates": list(candidates),
    }


def _candidate(identifier: str, pillar: str, title: str, url: str, summary: str = "Summary") -> dict[str, str]:
    return {
        "candidate_id": identifier,
        "pillar": pillar,
        "title": title,
        "summary": summary,
        "url": url,
    }


def _reasons(plan: dict) -> dict[str, tuple[str, str]]:
    return {
        item["candidate_id"]: (item["disposition"], item["reason"])
        for item in plan["decisions"]
    }


def test_plan_prioritizes_a_even_when_b_is_listed_first_and_normalizes_urls(tmp_path):
    sources = tmp_path / "sources"
    _write_report(sources, "2026-08-03", _item("Old", "Old", "https://old.example/item"))
    database = _registry(tmp_path, sources)
    payload = _payload(
        _candidate("b-first", "B", "Different", "https://EXAMPLE.com/item/?utm_source=x#part"),
        _candidate("a-owner", "A", "Owner", "https://example.com/item"),
        _candidate("a-later", "A", "Later", "https://example.com/item/"),
    )

    plan = plan_registry_selection(database, sources, payload)

    assert [item["candidate_id"] for item in plan["decisions"]] == [
        "a-owner", "a-later", "b-first"
    ]
    assert _reasons(plan) == {
        "a-owner": ("selected", "new_article"),
        "a-later": ("rejected", "same_pillar_canonical_url"),
        "b-first": ("rejected", "cross_pillar_canonical_url"),
    }


def test_policy_title_and_history_semantics_are_fail_closed(tmp_path):
    sources = tmp_path / "sources"
    _write_report(
        sources,
        "2026-08-03",
        _item("Historical title", "Original representation", "https://history.example/story"),
    )
    database = _registry(tmp_path, sources)
    payload = _payload(
        _candidate("a-history", "A", "Rewritten", "https://history.example/story", "New report summary"),
        _candidate("a-root", "A", "Root", "https://root.example/"),
        _candidate("b-root-copy", "B", "Root elsewhere", "https://root.example/?utm_medium=x"),
        _candidate("a-title", "A", "Exact  Title", "https://new.example/a"),
        _candidate("a-title-copy", "A", " exact title ", "https://new.example/b"),
        _candidate("b-title-copy", "B", "EXACT TITLE", "https://new.example/c"),
        _candidate("same-history-title", "B", "Historical title", "https://new.example/d"),
        _candidate("topic", "B", "Topic", "https://iais.org/activities-topics/climate-risk"),
    )

    plan = plan_registry_selection(database, sources, payload)

    assert _reasons(plan) == {
        "a-history": ("rejected", "historical_url_seen"),
        "a-root": ("rejected", "publication_ineligible"),
        "a-title": ("selected", "new_article"),
        "a-title-copy": ("rejected", "same_run_canonical_title"),
        "b-root-copy": ("rejected", "publication_ineligible"),
        "b-title-copy": ("rejected", "same_run_canonical_title"),
        "same-history-title": ("selected", "new_article"),
        "topic": ("rejected", "publication_ineligible"),
    }
    serialized = json.dumps(plan).casefold()
    assert "history.example" not in serialized
    assert "new.example" not in serialized
    assert "new report summary" not in serialized


def test_input_parser_rejects_duplicate_keys_unknown_fields_bounds_and_bad_values(tmp_path):
    valid = _payload(_candidate("safe-id", "A", "Title", "https://example.com/story"))
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(valid), encoding="utf-8")
    assert load_selection_input(input_path) == valid

    bad_payloads = [
        '{"schema_version":"registry-selection-input.v1","report_date":"2026-08-10","report_date":"2026-08-10","candidates":[]}',
        json.dumps({**valid, "unknown": True}),
        json.dumps(_payload(_candidate("unsafe id", "A", "Title", "https://example.com/story"))),
        json.dumps(_payload(_candidate("safe", "C", "Title", "https://example.com/story"))),
        json.dumps(_payload(_candidate("safe", "A", "", "https://example.com/story"))),
        json.dumps(_payload(_candidate("safe", "A", "Title", "file:///etc/passwd"))),
        json.dumps(_payload(*[_candidate(f"id-{index}", "A", "Title", f"https://example.com/{index}") for index in range(501)])),
    ]
    for index, raw in enumerate(bad_payloads):
        path = tmp_path / f"bad-{index}.json"
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(RegistryInputError):
            load_selection_input(path)

    too_large = tmp_path / "large.json"
    too_large.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(RegistryInputError, match="size limit"):
        load_selection_input(too_large)

    with pytest.raises(RegistryInputError, match="regular file"):
        load_selection_input(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a b",
        "https://example.com/a\u0000b",
        "https://[::1",
        "https://example.com:bad/story",
        "https://example.com:99999/story",
        "https://example.com:0/story",
        "https://example.com/%ZZ",
        "https://example.com/%A",
        "https://example.com\\story/path",
        "https://example.com|evil/story",
        "https://example.com/story[raw]",
        "https://example.com/story?q=[raw]",
        "https://example.com/story#part[raw]",
        "https://example.com/café",
        "https://café.example/story",
        "https://example.com/%2fstory",
        "https://example.com/%41",
        "https://example.com/%7Euser",
        "https://example.com:/story",
        "http://example.com:80/story",
        "https://example.com:443/story",
        "https://example.com/a/./b",
        "https://example.com/a/../b",
        "https://%C3%A9.example/story",
        "https://example.com./story",
        "https://empty..example/story",
        f"https://{'a' * 64}.example/story",
        f"https://{'a.' * 127}a/story",
        "https://under_score.example/story",
        "https://-leading.example/story",
        "https://trailing-.example/story",
        "https://xn--a.example/story",
        "https://[fe80::1%25eth0]/story",
        "https://[2001:DB8::1]/story",
        "https://[2001:0db8::1]/story",
        "https://[v1.test%25zone]/story",
        "https://[v1.]/story",
        "https://[V1.test]/story",
        "https://127.1/story",
        "https://127.0.1/story",
        "https://127.0.0.01/story",
        "https://2130706433/story",
        "https://0x7f000001/story",
        "https://0177.0.0.1/story",
        "https://0x7f.0.0.1/story",
        "https://example.com:08443/story",
        "https://[2001:db8::1]:08443/story",
        "https://[v1.test]:08443/story",
        "https://xn--ab-0ea.example/story",
        "https://xn--ab-j1t.example/story",
    ],
)
def test_direct_planner_rejects_malformed_urls_with_static_error(url):
    payload = _payload(_candidate("unsafe-url", "A", "Title", url))

    with pytest.raises(RegistryInputError, match="selection candidate URL is invalid") as caught:
        plan_selection(payload, historical_urls=())

    assert url not in str(caught.value)


def test_direct_planner_accepts_ipv6_authority_and_percent_encoded_unicode():
    payload = _payload(
        _candidate("ipv6", "A", "IPv6", "https://[2001:db8::1]:8443/story"),
        _candidate(
            "encoded-unicode",
            "B",
            "Encoded Unicode",
            "https://example.com/caf%C3%A9",
        ),
        _candidate(
            "idna-host",
            "B",
            "IDNA host",
            "https://xn--r8jz45g.example/story",
        ),
        _candidate("ipvfuture", "B", "IPvFuture", "https://[v1.test]/story"),
        _candidate(
            "ipvfuture-case",
            "B",
            "IPvFuture case",
            "https://[vA.TeSt]/case",
        ),
        _candidate("duplicate-slashes", "B", "Double slash", "https://example.com/a//b"),
        _candidate("single-slash", "B", "Single slash", "https://example.com/a/b"),
        _candidate("query-ab", "B", "Query AB", "https://example.com/q?a=1&b=2"),
        _candidate("query-ba", "B", "Query BA", "https://example.com/q?b=2&a=1"),
        _candidate("path-upper", "B", "Path upper", "https://example.com/Case"),
        _candidate("path-lower", "B", "Path lower", "https://example.com/case"),
        _candidate("reserved-encoded", "B", "Encoded slash", "https://example.com/a%2Fb"),
        _candidate("plain-http", "B", "HTTP", "http://transport.example/story"),
        _candidate("plain-https", "B", "HTTPS", "https://transport.example/story"),
        _candidate("bare-host", "B", "Bare host", "https://host.example/story"),
        _candidate("www-host", "B", "WWW host", "https://www.host.example/story"),
        _candidate("no-port", "B", "No port", "https://port.example/story"),
        _candidate("nondefault-port", "B", "Nondefault port", "https://port.example:8443/story"),
        _candidate("uppercase-host", "B", "Uppercase host", "https://UPPER.example/story"),
        _candidate("ipv4", "B", "IPv4", "https://192.0.2.1/story"),
        _candidate(
            "numeric-dns",
            "B",
            "Numeric DNS labels",
            "https://1.2.3.example/story",
        ),
        _candidate("idna-sharp-s", "B", "IDNA sharp s", "https://xn--fa-hia.de/story"),
        _candidate(
            "idna-strasse",
            "B",
            "IDNA strasse",
            "https://xn--strae-oqa.de/story",
        ),
    )

    plan = plan_selection(payload, historical_urls=())

    assert _reasons(plan) == {
        "ipv6": ("selected", "new_article"),
        "encoded-unicode": ("selected", "new_article"),
        "idna-host": ("selected", "new_article"),
        "ipvfuture": ("selected", "new_article"),
        "ipvfuture-case": ("selected", "new_article"),
        "duplicate-slashes": ("selected", "new_article"),
        "single-slash": ("selected", "new_article"),
        "query-ab": ("selected", "new_article"),
        "query-ba": ("selected", "new_article"),
        "path-upper": ("selected", "new_article"),
        "path-lower": ("selected", "new_article"),
        "reserved-encoded": ("selected", "new_article"),
        "plain-http": ("selected", "new_article"),
        "plain-https": ("selected", "new_article"),
        "bare-host": ("selected", "new_article"),
        "www-host": ("selected", "new_article"),
        "no-port": ("selected", "new_article"),
        "nondefault-port": ("selected", "new_article"),
        "uppercase-host": ("selected", "new_article"),
        "ipv4": ("selected", "new_article"),
        "numeric-dns": ("selected", "new_article"),
        "idna-sharp-s": ("selected", "new_article"),
        "idna-strasse": ("selected", "new_article"),
    }


def test_noncanonical_unreserved_alias_cannot_bypass_same_run_or_history():
    payload = _payload(
        _candidate("a-owner", "A", "Owner", "https://example.com/~user"),
        _candidate("b-alias", "B", "Alias", "https://example.com/%7Euser"),
    )

    with pytest.raises(RegistryInputError, match="selection candidate URL is invalid"):
        plan_selection(payload, historical_urls={"https://example.com/~user"})


def test_all_tracked_historical_urls_satisfy_canonical_lexical_policy():
    source_dir = Path(__file__).resolve().parents[1] / "sources"
    reports = parse_report_directory(source_dir)
    articles = [
        article
        for report in reports
        for article in report.articles
    ]

    assert reports
    assert articles
    for article in articles:
        _validate_public_http_url(article.url)


def test_idna2008_runtime_dependency_is_directly_declared():
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text(encoding="utf-8").splitlines()

    assert "idna>=3,<4" in requirements


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://[::1",
        "https://example.com|evil/story",
        "https://example.com\\story",
        "https://example.com/story[raw]",
        "https://example.com/story?q=[raw]",
        "https://example.com/%2fstory",
        "https://example.com/%41",
        "https://example.com:/story",
        "https://example.com:443/story",
        "https://example.com/a/../b",
        "https://example.com./story",
        "https://under_score.example/story",
        "https://xn--a.example/story",
        "https://[fe80::1%25eth0]/story",
        "https://[2001:DB8::1]/story",
        "https://127.1/story",
        "https://2130706433/story",
        "https://127.0.0.01/story",
        "https://example.com:08443/story",
        "https://[2001:db8::1]:08443/story",
        "https://xn--ab-0ea.example/story",
        "https://xn--ab-j1t.example/story",
    ],
)
def test_malformed_historical_url_has_static_build_failure(tmp_path, unsafe_url):
    sources = tmp_path / "sources"
    report = _write_report(
        sources, "2026-08-03", _item("Old", "Old", "https://old.example/item")
    )
    database = _registry(tmp_path, sources)
    report.write_text(
        _weekly("2026-08-03", _item("Old", "Old", unsafe_url)),
        encoding="utf-8",
    )
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE reports SET report_sha256 = ? WHERE report_date = '2026-08-03'",
        (digest,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        RegistryBuildError, match="^registry source report history is invalid$"
    ) as caught:
        plan_registry_selection(database, sources, _payload())

    assert unsafe_url not in str(caught.value)


@pytest.mark.parametrize(
    "mode",
    ["registry-report-missing-source", "source-report-missing-registry", "extra-registry-report"],
)
def test_plan_requires_exact_report_history_synchronization(tmp_path, mode):
    sources = tmp_path / "sources"
    _write_report(
        sources, "2026-08-03", _item("First", "First", "https://first.example/item")
    )
    if mode == "registry-report-missing-source":
        second = _write_report(
            sources,
            "2026-08-10",
            _item("Second", "Second", "https://second.example/item"),
        )
    database = _registry(tmp_path, sources)

    if mode == "registry-report-missing-source":
        second.unlink()
    elif mode == "source-report-missing-registry":
        _write_report(
            sources,
            "2026-08-10",
            _item("Second", "Second", "https://second.example/item"),
        )
    else:
        connection = sqlite3.connect(database)
        connection.execute(
            """
            INSERT INTO reports(
                report_id, report_date, filename, report_title, report_sha256,
                cadence, report_format, sites_checked, sites_succeeded,
                sites_failed, parse_warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "report-extra",
                "2026-08-10",
                "climate-monitor-2026-08-10.md",
                "Extra",
                "0" * 64,
                "weekly",
                "weekly-pillars-v1",
                0,
                0,
                0,
                "[]",
            ),
        )
        connection.commit()
        connection.close()

    with pytest.raises(
        RegistryInputError,
        match="^registry and source history are not synchronized$",
    ):
        plan_registry_selection(database, sources, _payload())


@pytest.mark.parametrize(
    ("mode", "error_type", "message"),
    [
        ("missing", RegistryInputError, "registry database does not exist"),
        ("corrupt", RegistryBuildError, "registry database is unreadable or corrupt"),
        ("v2", RegistryInputError, "registry schema contract is invalid"),
        ("future", RegistryInputError, "registry schema contract is invalid"),
        ("contract", RegistryInputError, "registry schema contract is invalid"),
        ("out-of-sync", RegistryInputError, "registry and source history are not synchronized"),
    ],
)
def test_plan_rejects_unusable_or_unsynchronized_database(
    tmp_path, mode, error_type, message
):
    sources = tmp_path / "sources"
    report = _write_report(sources, "2026-08-03", _item("Old", "Old", "https://old.example/item"))
    database = tmp_path / "registry.sqlite3"
    if mode == "missing":
        pass
    elif mode == "corrupt":
        database.write_bytes(b"not sqlite")
    elif mode in {"v2", "future"}:
        connection = sqlite3.connect(database)
        apply_migrations(connection, target_version=2 if mode == "v2" else None)
        if mode == "future":
            connection.execute("PRAGMA user_version = 99")
        connection.commit()
        connection.close()
    else:
        database = _registry(tmp_path, sources)
        if mode == "contract":
            connection = sqlite3.connect(database)
            connection.execute("DROP TABLE article_enrichments")
            connection.commit()
            connection.close()
        else:
            report.write_text(_weekly("2026-08-03", _item("Changed", "Changed", "https://old.example/item")), encoding="utf-8")

    with pytest.raises(error_type, match=f"^{message}$"):
        plan_registry_selection(database, sources, _payload())


@pytest.mark.parametrize("mode", ["hollow", "extra"])
def test_plan_requires_exact_article_url_graph_parity(tmp_path, mode):
    sources = tmp_path / "sources"
    _write_report(sources, "2026-08-03", _item("Old", "Old", "https://old.example/item"))
    database = _registry(tmp_path, sources)
    connection = sqlite3.connect(database)
    if mode == "hollow":
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in ("report_appearances", "discoveries", "url_aliases", "article_versions", "articles"):
            connection.execute(f"DELETE FROM {table}")
    else:
        connection.execute(
            "INSERT INTO sources VALUES ('source-extra', 'extra.example', 'extra.example', '2026-08-03', '2026-08-03')"
        )
        connection.execute(
            """
            INSERT INTO articles(
                article_id, canonical_url, source_id, first_seen, last_seen,
                document_kind, publication_eligible, display_policy
            ) VALUES (
                'article-extra', 'https://extra.example/story', 'source-extra',
                '2026-08-03', '2026-08-03', 'article', 1, 'summary_excerpt'
            )
            """
        )
    connection.commit()
    connection.close()

    with pytest.raises(
        RegistryInputError, match="^registry article graph and source history are not synchronized$"
    ):
        plan_registry_selection(database, sources, _payload())


def test_plan_is_byte_read_only_and_creates_no_sidecars(tmp_path):
    sources = tmp_path / "sources"
    _write_report(sources, "2026-08-03", _item("Old", "Old", "https://old.example/item"))
    database = _registry(tmp_path, sources)
    before_bytes = database.read_bytes()
    before_stat = database.stat()
    before_listing = sorted(path.name for path in database.parent.iterdir())

    plan_registry_selection(
        database,
        sources,
        _payload(_candidate("fresh", "A", "Fresh", "https://new.example/story")),
    )

    after_stat = database.stat()
    assert database.read_bytes() == before_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert sorted(path.name for path in database.parent.iterdir()) == before_listing
    assert not any(Path(f"{database}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))


def test_plan_rejects_active_sidecars_and_database_inside_source_history(tmp_path):
    sources = tmp_path / "sources"
    _write_report(sources, "2026-08-03", _item("Old", "Old", "https://old.example/item"))
    database = _registry(tmp_path, sources)
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"active")
    with pytest.raises(RegistryInputError, match="sidecars"):
        plan_registry_selection(database, sources, _payload())
    assert wal.read_bytes() == b"active"

    wal.unlink()
    inside = sources / "registry.sqlite3"
    inside.write_bytes(database.read_bytes())
    with pytest.raises(RegistryInputError, match="external"):
        plan_registry_selection(inside, sources, _payload())


def test_2026_08_17_regression_selects_a9_and_only_five_new_b_items(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    sources = tmp_path / "sources"
    sources.mkdir()
    for path in sorted((repo / "sources").glob("climate-monitor-*.md")):
        if path.name < "climate-monitor-2026-08-17.md":
            (sources / path.name).write_bytes(path.read_bytes())
    database = _registry(tmp_path, sources)
    report = parse_historical_report(repo / "sources" / "climate-monitor-2026-08-17.md")
    payload = _payload(
        *[
            _candidate(f"item-{index:02d}", article.pillar or "B", article.title, article.url, article.summary)
            for index, article in enumerate(report.articles, 1)
        ],
        day="2026-08-17",
    )

    plan = plan_registry_selection(database, sources, payload)

    a = [item for item in plan["decisions"] if item["pillar"] == "A"]
    b = [item for item in plan["decisions"] if item["pillar"] == "B"]
    assert sum(item["disposition"] == "selected" for item in a) == 9
    assert sum(item["disposition"] == "selected" for item in b) == 5
    assert sum(item["disposition"] == "rejected" for item in b) == 12
    assert _reasons(plan)["item-22"] == ("rejected", "publication_ineligible")
