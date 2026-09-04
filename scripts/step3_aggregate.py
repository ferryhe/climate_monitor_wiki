#!/usr/bin/env python3
"""Step 3: Aggregate + Dedup.

Reads Pillar A (article_changes_<DATE>.json) and Pillar B (pillar_b_<DATE>.json),
deduplicates by URL and near-identical titles, and outputs a unified candidate list.

This is a deterministic script — no LLM, no randomness.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_registry.classification import classify_document
from climate_registry.errors import RegistryInputError
from climate_registry.selection import (
    MAX_SUMMARY,
    MAX_TITLE,
    MAX_URL,
    _validate_public_http_url,
)

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))

PILLAR_A_ITEM_FIELDS = frozenset({"title", "url", "categories"})
PILLAR_B_ITEM_FIELDS = frozenset({"title", "url", "source", "summary"})


def _row_reasons(item: object, *, allowed_fields: frozenset[str]) -> list[str]:
    if not isinstance(item, dict):
        return ["invalid_record_type"]

    reasons = []
    if set(item) - allowed_fields:
        reasons.append("unexpected_fields")

    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        reasons.append("missing_title")
    elif len(title) > MAX_TITLE:
        reasons.append("invalid_title")

    url = item.get("url")
    if not isinstance(url, str) or not url.strip():
        reasons.append("missing_url")
    elif len(url) > MAX_URL:
        reasons.append("invalid_url")
    else:
        try:
            _validate_public_http_url(url)
        except RegistryInputError:
            reasons.append("invalid_url")
        else:
            if not classify_document(url).publication_eligible:
                reasons.append("publication_ineligible_url")

    summary = item.get("summary", "")
    if not isinstance(summary, str) or len(summary) > MAX_SUMMARY:
        reasons.append("invalid_summary")
    if "categories" in item:
        categories = item["categories"]
        if (
            not isinstance(categories, list)
            or not categories
            or any(not isinstance(value, str) or not value.strip() for value in categories)
        ):
            reasons.append("invalid_categories")
    return reasons


def _record_errors(
    errors: dict[str, Counter[str]], artifact: str, reasons: list[str]
) -> None:
    if reasons:
        errors.setdefault(artifact, Counter()).update(reasons)
        errors[artifact]["__rows__"] += 1


def _load_pillar_a(
    path: Path, *, report_date: str, errors: dict[str, Counter[str]]
) -> list[dict]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("Pillar A artifact must be a JSON object")
    if payload.get("date") != report_date:
        raise ValueError("Pillar A artifact date does not match --date")
    if payload.get("pillar") != "A":
        raise ValueError("Pillar A artifact must declare pillar A")
    groups = payload.get("articles")
    if not isinstance(groups, list):
        raise ValueError("Pillar A artifact articles must be an array")

    retained = []
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            _record_errors(errors, path.name, ["invalid_org_group"])
            continue
        org = group.get("org")
        org_valid = isinstance(org, str) and bool(org.strip())
        for item_index, item in enumerate(group["items"]):
            reasons = _row_reasons(item, allowed_fields=PILLAR_A_ITEM_FIELDS)
            if not org_valid:
                reasons.append("invalid_org")
            _record_errors(errors, path.name, reasons)
            if reasons:
                continue
            retained.append({
                "title": item["title"],
                "url": item["url"],
                "source": org,
                "pillar": "A",
                "summary": "",
                "org": org,
                "provenance": {
                    "artifact": path.name,
                    "record": f"articles[{group_index}].items[{item_index}]",
                },
            })
    return retained


def _load_pillar_b(path: Path, *, errors: dict[str, Counter[str]]) -> list[dict]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, list):
        raise ValueError("Pillar B artifact must be a JSON array")

    retained = []
    for item_index, item in enumerate(payload):
        reasons = _row_reasons(item, allowed_fields=PILLAR_B_ITEM_FIELDS)
        if isinstance(item, dict):
            if set(item) != PILLAR_B_ITEM_FIELDS:
                if "unexpected_fields" not in reasons:
                    reasons.append("unexpected_fields")
            if item.get("source") != "web":
                reasons.append("invalid_source")
        _record_errors(errors, path.name, reasons)
        if reasons:
            continue
        retained.append({
            **item,
            "pillar": "B",
            "provenance": {
                "artifact": path.name,
                "record": f"[{item_index}]",
            },
        })
    return retained


def _format_errors(errors: dict[str, Counter[str]]) -> str:
    rows = sum(counts["__rows__"] for counts in errors.values())
    artifacts = []
    for artifact, counts in errors.items():
        reason_text = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(counts.items())
            if reason != "__rows__"
        )
        artifacts.append(f"{artifact}: {counts['__rows__']} ({reason_text})")
    return f"{rows} malformed article rows: " + "; ".join(artifacts)


def normalize_url(url: str) -> str:
    """Normalize URL for dedup."""
    url = url.strip().lower()
    url = re.sub(r"^https?://(www\.)?", "", url)
    url = re.sub(r"[?#].*$", "", url)
    return url.rstrip("/")


def normalize_title(title: str) -> str:
    """Normalize title for near-duplicate detection."""
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t


def dedup_items(items: list[dict]) -> list[dict]:
    """Deduplicate by normalized URL and near-identical titles.

    When a URL duplicate arrives, merge summary/keywords from the duplicate
    into the kept record instead of silently dropping richer data (Pillar B
    search results often carry a summary the Pillar A record lacks).
    Titles are preserved verbatim — title-casing only happens upstream for
    URL-slug-derived titles.
    """
    seen_urls = {}
    seen_titles = set()
    result = []

    for item in items:
        url = normalize_url(item.get("url", ""))
        title = item.get("title", "").strip()
        # Dedupe on a normalized key but keep the verbatim title in output.
        title_key = normalize_title(title) if title else ""

        if url and url in seen_urls:
            kept = seen_urls[url]
            if not kept.get("summary") and item.get("summary"):
                kept["summary"] = item["summary"]
            if item.get("keywords") and not kept.get("keywords"):
                kept["keywords"] = item["keywords"]
            continue
        if title_key and title_key in seen_titles:
            continue

        if url:
            seen_urls[url] = item
        if title_key:
            seen_titles.add(title_key)
        result.append(item)

    return result


def main():
    parser = argparse.ArgumentParser(description="Aggregate + Dedup")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        parsed_date = date.fromisoformat(args.date)
    except ValueError:
        print(f"ERROR: invalid --date {args.date!r} (expected YYYY-MM-DD)")
        return 1
    if parsed_date.isoformat() != args.date:
        print(f"ERROR: invalid --date {args.date!r} (expected YYYY-MM-DD)")
        return 1
    args.date = parsed_date.isoformat()

    out_path = args.output or (REPORTS / f"aggregated_{args.date}.json")
    pa_path = REPORTS / f"article_changes_{args.date}.json"
    pb_path = REPORTS / f"pillar_b_{args.date}.json"
    try:
        if out_path.resolve() in {pa_path.resolve(), pb_path.resolve()}:
            print(f"ERROR: aggregate output must not overwrite an input artifact: {out_path}")
            return 1
        out_path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"ERROR: cannot invalidate stale aggregate {out_path}: {exc}")
        return 1

    errors: dict[str, Counter[str]] = {}

    # Read Pillar A
    if pa_path.exists():
        try:
            pa_items = _load_pillar_a(
                pa_path, report_date=args.date, errors=errors
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"ERROR: invalid {pa_path.name}: {exc}")
            return 1
    else:
        pa_items = []
        print(f"WARNING: Pillar A file not found: {pa_path}")

    # Read Pillar B
    if pb_path.exists():
        try:
            pb_items = _load_pillar_b(pb_path, errors=errors)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"ERROR: invalid {pb_path.name}: {exc}")
            return 1
    else:
        pb_items = []
        print(f"WARNING: Pillar B file not found: {pb_path}")

    if errors:
        print(f"ERROR: {_format_errors(errors)}")
        return 1

    # Aggregate
    all_items = pa_items + pb_items
    print(f"Aggregate: {len(pa_items)} Pillar A + {len(pb_items)} Pillar B = {len(all_items)} total")

    # Dedup
    deduped = dedup_items(all_items)
    print(f"After dedup: {len(deduped)} items ({len(all_items) - len(deduped)} removed)")

    output = {
        "date": args.date,
        "pillar_a_count": len(pa_items),
        "pillar_b_count": len(pb_items),
        "total_before_dedup": len(all_items),
        "total_after_dedup": len(deduped),
        "items": deduped
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"OK: {out_path}")


if __name__ == "__main__":
    raise SystemExit(main())
