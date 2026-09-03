#!/usr/bin/env python3
"""Step 6: Render PDF from markdown with MD executive summary."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))
ARTIFACTS = Path(os.environ.get("CLIMATE_ARTIFACT_ROOT", "/home/ubuntu/climate_delivery_artifacts"))
PYTHON = Path(os.environ.get("CLIMATE_WIKI_PYTHON", str(HOME / ".venv" / "bin" / "python")))
if not PYTHON.exists():
    # Fall back to the interpreter running this script (e.g. Render, where the
    # /home/ubuntu venv path does not exist).
    PYTHON = Path(sys.executable)
REPO = HOME


def last_monday():
    """Get the most recent Monday."""
    today = date.today()
    if today.weekday() == 0:
        return today
    return today - timedelta(days=today.weekday())


def extract_md_executive_summary(md_path):
    """Extract executive summary paragraphs from MD."""
    text = md_path.read_text(encoding='utf-8')
    lines = text.split('\n')

    # Find section
    in_section = False
    summary_lines = []
    for line in lines:
        if line.startswith('## ') and 'Executive Summary' in line:
            in_section = True
            continue
        if in_section and line.startswith('## '):
            break
        if in_section:
            stripped = line.strip()
            if stripped and not stripped.startswith('- Sites checked:') and not stripped.startswith('- Monitored window:') and not stripped.startswith('- Pillar B search window:') and not stripped.startswith('- Total detected changes:'):
                summary_lines.append(stripped)

    return summary_lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=last_monday().isoformat())
    parser.add_argument(
        "--allow-offcycle",
        action="store_true",
        help="Allow non-Monday report dates (manual re-runs). Off by default.",
    )
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

    md_path = REPORTS / f"climate-monitor-{args.date}.md"
    if not md_path.exists():
        print(f"ERROR: markdown not found: {md_path}")
        return 1

    data = md_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()

    out_dir = ARTIFACTS / args.date / sha
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"climate-monitor-{args.date}.pdf"

    with tempfile.TemporaryDirectory(prefix="pdf-render-") as tmpdir:
        tmp_md = Path(tmpdir) / f"climate-monitor-{args.date}.md"
        shutil.copy2(md_path, tmp_md)
        tmp_summary = Path(tmpdir) / "summary.json"
        tmp_pdf = Path(tmpdir) / f"climate-monitor-{args.date}.pdf"

        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)

        # Step 6a: Build summary JSON
        print("Running summarize...")
        summarize_args = [str(PYTHON), "-m", "climate_delivery", "summarize",
             "--report", str(tmp_md.resolve()),
             "--output", str(tmp_summary.resolve())]
        if args.allow_offcycle:
            summarize_args.append("--allow-offcycle")
        result = subprocess.run(
            summarize_args,
            capture_output=True, text=True, timeout=120, env=env,
        )
        if result.returncode != 0:
            print(f"ERROR: summarize failed (exit {result.returncode}): {result.stderr}")
            return 1
        print("OK: summary.json generated")

        # Inject MD executive summary into summary JSON
        md_summary = extract_md_executive_summary(md_path)
        if md_summary:
            summary = json.loads(tmp_summary.read_text())
            summary["executive_summary"] = md_summary
            tmp_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

        # Step 6b: Render PDF
        print("Running render-pdf...")
        render_args = [str(PYTHON), "-m", "climate_delivery", "render-pdf",
             "--summary", str(tmp_summary.resolve()),
             "--output", str(tmp_pdf.resolve())]
        if args.allow_offcycle:
            render_args.append("--allow-offcycle")
        result = subprocess.run(
            render_args,
            capture_output=True, text=True, timeout=120, env=env,
        )
        if result.returncode != 0:
            print(f"ERROR: render-pdf failed (exit {result.returncode}): {result.stderr}")
            return 1

        shutil.copy2(tmp_pdf, pdf_path)
        with pdf_path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                pdf_path.unlink()
                print("ERROR: render-pdf produced a file without a %PDF- header")
                return 1
        print(f"OK: {pdf_path} ({pdf_path.stat().st_size} bytes, %PDF- verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
