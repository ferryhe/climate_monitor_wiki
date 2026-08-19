from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_delivery.artifacts import load_report_artifact
from climate_registry.read_api import RegistryReader
from climate_registry.reports import parse_historical_report


SAFE_SYNC_EXIT_CODES = frozenset({0, 6})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _JobError(RuntimeError):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Draft 10:30 weekly Registry sync, reload, and verification runner."
    )
    parser.add_argument("--date", required=True)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--lock-file", required=True, type=Path)
    parser.add_argument("--publisher-ledger-dir", required=True, type=Path)
    parser.add_argument("--metadata-dir", type=Path)
    parser.add_argument("--base-url", default=os.getenv("API_BASE_URL", "http://localhost:8501"))
    parser.add_argument("--expected-api-host", default=os.getenv("SITE_HOST"))
    parser.add_argument("--capture-timeout", type=float, default=30.0)
    parser.add_argument("--process-timeout", type=float, default=1800.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    return parser


def _base_url(value: str, expected_host: str | None = None) -> str:
    try:
        parsed = parse.urlsplit(value)
    except ValueError as exc:
        raise _JobError("invalid_base_url") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise _JobError("invalid_base_url")
    try:
        port = parsed.port
    except ValueError as exc:
        raise _JobError("invalid_base_url") from exc
    hostname = parsed.hostname.lower()
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname == "localhost"
    if not is_loopback:
        if parsed.scheme != "https" or not expected_host:
            raise _JobError("invalid_base_url")
        expected = expected_host.strip().lower()
        try:
            expected_normalized = str(ipaddress.ip_address(expected.strip("[]")))
            actual_normalized = str(ipaddress.ip_address(hostname))
        except ValueError:
            if not expected or any(character in expected for character in "/@?#:"):
                raise _JobError("invalid_base_url")
            expected_normalized = expected
            actual_normalized = hostname
        if actual_normalized != expected_normalized:
            raise _JobError("invalid_base_url")
    authority = hostname
    if ":" in authority and not authority.startswith("["):
        authority = f"[{authority}]"
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


def _sync_command(
    args: argparse.Namespace,
    *,
    dry_run: bool,
    expected_report_sha256: str | None = None,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "climate_registry",
        "weekly-sync",
        "--date",
        args.date,
        "--source-dir",
        str(args.source_dir),
        "--database",
        str(args.database),
        "--artifact-root",
        str(args.artifact_root),
        "--backup-dir",
        str(args.backup_dir),
        "--lock-file",
        str(args.lock_file),
        "--publisher-ledger-dir",
        str(args.publisher_ledger_dir),
        "--timeout",
        str(args.capture_timeout),
    ]
    if dry_run:
        command.append("--dry-run")
    if expected_report_sha256 is not None:
        command.extend(("--expected-report-sha256", expected_report_sha256))
    if args.metadata_dir is not None:
        command.extend(("--metadata-dir", str(args.metadata_dir)))
    return command


def _nonnegative_int(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _JobError("invalid_sync_result")
    return value


def _validate_sync_result(
    payload: Any,
    *,
    target_date: str,
    dry_run: bool,
    returncode: int,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _JobError("invalid_sync_result")
    status = payload.get("status")
    coverage_status = payload.get("coverage_status")
    if (
        status not in {"ok", "no-op"}
        or coverage_status not in {"ok", "partial_with_validated_fallback"}
        or payload.get("date") != target_date
        or payload.get("dry_run") is not dry_run
        or not isinstance(payload.get("report_sha256"), str)
        or _SHA256.fullmatch(payload["report_sha256"]) is None
        or payload.get("promotion") not in {"performed", "not-needed"}
        or not isinstance(payload.get("reload_required"), bool)
        or not isinstance(payload.get("would_promote"), bool)
        or (returncode == 6) != (status == "no-op")
    ):
        raise _JobError("invalid_sync_result")
    for name in (
        "reports_added",
        "articles_added",
        "articles_captured",
        "articles_failed",
        "articles_fallback",
        "articles_unresolved",
        "would_add_reports",
        "would_add_articles",
        "would_capture_count",
        "would_fallback_count",
        "target_article_count",
        "target_eligible_article_count",
    ):
        _nonnegative_int(payload, name)
    failure_classes = payload.get("fallback_failure_classes")
    if (
        not isinstance(failure_classes, dict)
        or set(failure_classes) - {"http_403_publisher_bot_wall"}
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in failure_classes.values())
        or not isinstance(payload.get("promotion_with_fallback"), bool)
        or payload["promotion_with_fallback"]
        != (payload["promotion"] == "performed" and payload["articles_fallback"] > 0)
        or payload["articles_unresolved"] != 0
        or (
            coverage_status == "ok"
            and any(payload[name] != 0 for name in (
                "articles_failed", "articles_fallback", "articles_unresolved"
            ))
        )
        or (
            coverage_status == "partial_with_validated_fallback"
            and (
                dry_run
                or status != "ok"
                or payload["articles_fallback"] <= 0
                or payload["articles_failed"] != payload["articles_fallback"]
                or sum(failure_classes.values()) != payload["articles_fallback"]
            )
        )
    ):
        raise _JobError("invalid_sync_result")
    for name in ("database_sha256_before", "database_sha256_after"):
        if not isinstance(payload.get(name), str) or _SHA256.fullmatch(payload[name]) is None:
            raise _JobError("invalid_sync_result")
    target_article_ids = payload.get("target_article_ids")
    fallback_article_ids = payload.get("fallback_article_ids")
    if (
        not isinstance(target_article_ids, list)
        or len(target_article_ids) != payload["target_article_count"]
        or len(set(target_article_ids)) != len(target_article_ids)
        or any(
            not isinstance(article_id, str)
            or not article_id
            or len(article_id) > 128
            for article_id in target_article_ids
        )
    ):
        raise _JobError("invalid_sync_result")
    if (
        not isinstance(fallback_article_ids, list)
        or fallback_article_ids != sorted(fallback_article_ids)
        or len(fallback_article_ids) != payload["articles_fallback"]
        or len(set(fallback_article_ids)) != len(fallback_article_ids)
        or not set(fallback_article_ids) <= set(target_article_ids)
    ):
        raise _JobError("invalid_sync_result")
    backup_name = payload.get("backup_name")
    if payload["promotion"] == "performed":
        if (
            not isinstance(backup_name, str)
            or not backup_name
            or len(backup_name) > 255
            or Path(backup_name).name != backup_name
            or backup_name in {".", ".."}
        ):
            raise _JobError("invalid_sync_result")
    elif backup_name is not None:
        raise _JobError("invalid_sync_result")
    if dry_run:
        if payload["promotion"] != "not-needed" or payload["reload_required"]:
            raise _JobError("invalid_sync_result")
    elif (payload["promotion"] == "performed") != payload["reload_required"]:
        raise _JobError("invalid_sync_result")
    return payload


def _run_sync(
    args: argparse.Namespace,
    *,
    dry_run: bool,
    expected_report_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            _sync_command(
                args,
                dry_run=dry_run,
                expected_report_sha256=expected_report_sha256,
            ),
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.process_timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _JobError("weekly_sync_unavailable") from exc
    if completed.returncode not in SAFE_SYNC_EXIT_CODES:
        raise _JobError("dry_run_blocked" if dry_run else "sync_blocked")
    lines = completed.stdout.splitlines()
    if completed.stderr or len(lines) != 1:
        raise _JobError("invalid_sync_result")
    try:
        payload = json.loads(lines[0])
    except (json.JSONDecodeError, RecursionError) as exc:
        raise _JobError("invalid_sync_result") from exc
    return _validate_sync_result(
        payload,
        target_date=args.date,
        dry_run=dry_run,
        returncode=completed.returncode,
    )


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    timeout: float,
) -> dict[str, Any]:
    outbound = request.Request(url, method=method, headers=dict(headers or {}))
    class _RejectRedirects(request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            return None

    try:
        opener = request.build_opener(_RejectRedirects())
        with opener.open(outbound, timeout=timeout) as response:
            body = response.read(2 * 1024 * 1024 + 1)
    except (error.HTTPError, error.URLError, OSError, TimeoutError) as exc:
        raise _JobError("api_request_failed") from exc
    if len(body) > 2 * 1024 * 1024:
        raise _JobError("api_response_invalid")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _JobError("api_response_invalid") from exc
    if not isinstance(payload, dict):
        raise _JobError("api_response_invalid")
    return payload


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise _JobError("sha_verification_failed") from exc
    return digest.hexdigest()


def _verify_sha_binding(
    args: argparse.Namespace,
    sync_result: Mapping[str, Any],
) -> dict[str, Any]:
    expected = sync_result["report_sha256"]
    try:
        database_before = _stream_sha256(args.database)
        reader = RegistryReader(args.database, repository_root=ROOT)
        identity = reader.report_identity(args.date)
        report_payload = reader.report(args.date)
        registry_article_ids = [
            article.get("article_id")
            for article in report_payload.get("articles", [])
            if isinstance(article, dict)
        ]
        expected_filename = f"climate-monitor-{args.date}.md"
        if identity.filename != expected_filename:
            raise _JobError("sha_verification_failed")
        source_report = parse_historical_report(args.source_dir / expected_filename)
        artifact = load_report_artifact(
            args.artifact_root,
            report_date=identity.report_date,
            report_filename=identity.filename,
            report_title=identity.report_title,
            report_sha256=identity.report_sha256,
            include_pdf_bytes=False,
        )
        database_after = _stream_sha256(args.database)
    except _JobError:
        raise
    except Exception as exc:
        raise _JobError("sha_verification_failed") from exc
    if (
        database_before != database_after
        or database_after != sync_result["database_sha256_after"]
        or identity.report_date != args.date
        or identity.report_sha256 != expected
        or source_report.report_date != args.date
        or source_report.sha256 != expected
        or artifact is None
        or registry_article_ids != sync_result["target_article_ids"]
    ):
        raise _JobError("sha_verification_failed")
    return {
        "report_sha256": expected,
        "database_sha256": database_after,
        "target_article_count": sync_result["target_article_count"],
        "target_article_ids": list(sync_result["target_article_ids"]),
    }


def _verify_article(
    detail: Mapping[str, Any],
    *,
    article_id: str,
    target_date: str,
    fallback_article_ids: frozenset[str],
) -> None:
    enrichment = detail.get("enrichment")
    provenance = detail.get("metadata_provenance")
    latest_fetch = detail.get("latest_fetch")
    appearances = detail.get("appearances")
    common_invalid = (
        detail.get("article_id") != article_id
        or detail.get("publication_eligible") is not True
        or not all(
            _nonempty(detail.get(name))
            for name in ("title", "summary", "canonical_url", "original_url", "source", "publisher")
        )
        or not isinstance(detail.get("categories"), list)
        or not isinstance(detail.get("keywords"), list)
        or not isinstance(enrichment, dict)
        or not isinstance(latest_fetch, dict)
        or not isinstance(appearances, list)
        or not any(
            isinstance(item, dict) and item.get("report_date") == target_date
            for item in appearances
        )
    )
    summary_provenance = detail.get("summary_provenance")
    db_complete = (
        summary_provenance == "content_enrichment"
        and _nonempty(enrichment.get("summary"))
        and isinstance(enrichment.get("categories"), list)
        and isinstance(enrichment.get("keywords"), list)
        and _nonempty(enrichment.get("language"))
        and isinstance(enrichment.get("generator"), dict)
        and all(
            _nonempty(enrichment["generator"].get(name))
            for name in ("kind", "name", "version", "generated_at")
        )
        and provenance == {"categories": "content_enrichment", "keywords": "content_enrichment"}
        and (
            latest_fetch.get("fetch_status") in {"success", "not_modified"}
            or (
                article_id in fallback_article_ids
                and latest_fetch.get("fetch_status") == "failed"
                and latest_fetch.get("http_status") == 403
                and latest_fetch.get("error_code") == "http_error"
            )
        )
    )
    fallback_complete = (
        summary_provenance in {
            "source_report", "original_content_annotation",
            "official_replacement_annotation", "publisher_excerpt_annotation",
            "report_fallback_annotation",
        }
        and provenance == {"categories": summary_provenance, "keywords": summary_provenance}
        and bool(detail.get("categories"))
        and bool(detail.get("keywords"))
        and article_id in fallback_article_ids
        and latest_fetch.get("fetch_status") == "failed"
        and latest_fetch.get("http_status") == 403
        and latest_fetch.get("error_code") == "http_error"
    )
    if common_invalid or not (db_complete or fallback_complete):
        raise _JobError("article_detail_incomplete")


def _reload_and_verify(
    args: argparse.Namespace,
    *,
    base_url: str,
    sync_result: Mapping[str, Any],
    sha_binding: Mapping[str, Any],
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    reload_token = os.getenv("RELOAD_TOKEN", "")
    if reload_token:
        headers["x-reload-token"] = reload_token
    _request_json(
        f"{base_url}/api/reload",
        method="POST",
        headers=headers,
        timeout=args.request_timeout,
    )
    status = _request_json(
        f"{base_url}/api/registry/status", timeout=args.request_timeout
    )
    if status.get("available") is not True or status.get("latest_report_date") != args.date:
        raise _JobError("registry_latest_mismatch")

    report = _request_json(
        f"{base_url}/api/registry/reports/{parse.quote(args.date, safe='')}",
        timeout=args.request_timeout,
    )
    briefing = report.get("report_briefing")
    snapshot = briefing.get("monitoring_snapshot") if isinstance(briefing, dict) else None
    pdf = report.get("report_pdf")
    articles = report.get("articles")
    metric_names = (
        "sites_checked",
        "sites_succeeded",
        "sites_failed",
        "pillar_a_updates",
        "pillar_b_updates",
    )
    if (
        report.get("report_date") != args.date
        or not isinstance(briefing, dict)
        or not isinstance(briefing.get("executive_summary"), list)
        or not briefing["executive_summary"]
        or not all(_nonempty(item) for item in briefing["executive_summary"])
        or not isinstance(snapshot, dict)
        or any(
            isinstance(snapshot.get(name), bool)
            or not isinstance(snapshot.get(name), int)
            or snapshot[name] < 0
            for name in metric_names
        )
        or not isinstance(snapshot.get("notes"), list)
        or not all(_nonempty(item) for item in snapshot["notes"])
        or not isinstance(pdf, dict)
        or pdf.get("filename") != f"climate-monitor-{args.date}.pdf"
        or pdf.get("download_url") != f"/api/registry/reports/{args.date}/pdf"
        or not isinstance(articles, list)
        or [
            article.get("article_id") if isinstance(article, dict) else None
            for article in articles
        ]
        != sha_binding["target_article_ids"]
        or not articles
    ):
        raise _JobError("report_verification_failed")

    verified_ids: list[str] = []
    fallback_article_ids = frozenset(sync_result["fallback_article_ids"])
    for article in articles:
        if not isinstance(article, dict) or not _nonempty(article.get("article_id")):
            raise _JobError("report_verification_failed")
        candidate_id = article["article_id"]
        detail = _request_json(
            f"{base_url}/api/registry/articles/{parse.quote(candidate_id, safe='')}",
            timeout=args.request_timeout,
        )
        if detail.get("publication_eligible") is True:
            _verify_article(
                detail,
                article_id=candidate_id,
                target_date=args.date,
                fallback_article_ids=fallback_article_ids,
            )
            verified_ids.append(candidate_id)
    if not verified_ids:
        raise _JobError("article_detail_incomplete")

    return {
        "latest_report_date": args.date,
        "source_registry_artifact_sha_match": (
            sha_binding["report_sha256"] == sync_result["report_sha256"]
        ),
        "briefing": True,
        "monitoring_snapshot": True,
        "pdf": True,
        "article_count": len(articles),
        "sample_article_id": verified_ids[0],
        "verified_eligible_article_count": len(verified_ids),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dry_result: dict[str, Any] | None = None
    live_result: dict[str, Any] | None = None
    try:
        if (
            args.capture_timeout <= 0
            or args.process_timeout <= 0
            or args.request_timeout <= 0
        ):
            raise _JobError("invalid_runtime_options")
        normalized_base = _base_url(args.base_url, args.expected_api_host)
        dry_result = _run_sync(args, dry_run=True)
        live_result = _run_sync(
            args,
            dry_run=False,
            expected_report_sha256=dry_result["report_sha256"],
        )
        if dry_result["report_sha256"] != live_result["report_sha256"]:
            raise _JobError("sync_identity_changed")
        sha_binding = _verify_sha_binding(args, live_result)
        if live_result["promotion"] == "performed":
            verification = _reload_and_verify(
                args,
                base_url=normalized_base,
                sync_result=live_result,
                sha_binding=sha_binding,
            )
            reload_status = "performed"
        else:
            verification = {
                "source_registry_artifact_sha_match": True,
                "target_article_count": sha_binding["target_article_count"],
                "local_only": True,
            }
            reload_status = "not-needed"
        if _verify_sha_binding(args, live_result) != sha_binding:
            raise _JobError("sha_verification_failed")
        result = {
            "status": "ok",
            "date": args.date,
            "report_sha256": live_result["report_sha256"],
            "dry_run_status": dry_result["status"],
            "sync_status": live_result["status"],
            "promotion": live_result["promotion"],
            "backup_name": live_result["backup_name"],
            "database_sha256_before": live_result["database_sha256_before"],
            "database_sha256_after": live_result["database_sha256_after"],
            "reload": reload_status,
            "verification": verification,
        }
        code = 0
    except _JobError as exc:
        result = {
            "status": "failed",
            "date": args.date,
            "kind": exc.kind,
            "promotion": (
                live_result.get("promotion", "blocked")
                if live_result is not None
                else "blocked"
            ),
            "reload": "failed-or-unverified" if live_result is not None else "not-performed",
        }
        if live_result is not None:
            result["report_sha256"] = live_result.get("report_sha256")
            result["backup_name"] = live_result.get("backup_name")
            result["database_sha256_before"] = live_result.get(
                "database_sha256_before"
            )
            result["database_sha256_after"] = live_result.get(
                "database_sha256_after"
            )
        code = 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
