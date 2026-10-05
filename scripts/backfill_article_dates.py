#!/usr/bin/env python3
"""Append vetted historical Registry article date observations."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from climate_registry.article_dates import import_observations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="absolute path to a migrated Registry database copy")
    parser.add_argument("--observations", required=True, help="article-date-observations.v1 JSON file")
    parser.add_argument("--write", action="store_true", help="append observations; default is read-only dry run")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.observations).read_text(encoding="utf-8"))
        result = import_observations(args.database, payload, write=args.write)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"article date backfill failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"mode": "write" if args.write else "dry_run", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
