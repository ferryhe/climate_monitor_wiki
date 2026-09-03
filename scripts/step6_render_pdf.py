#!/usr/bin/env python3
"""Step 6: Render PDF from markdown with MD executive summary."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

REPORTS = Path("/home/ubuntu/climate_monitor_wiki/data/reports")
ARTIFACTS = Path("/home/ubuntu/climate_delivery_artifacts")
PYTHON = Path("/home/ubuntu/climate_monitor_wiki/.venv/bin/python")
REPO = Path("/home/ubuntu/climate_monitor_wiki")


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=last_monday().isoformat())
    args = parser.parse_args()

    md_path = REPORTS / f"climate-monitor-{args.date}.md"
    if not md_path.exists():
        print(f"ERROR: markdown not found: {md_path}")
        return

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
        result = subprocess.run(
            [str(PYTHON), "-m", "climate_delivery", "summarize",
             "--report", str(tmp_md.resolve()),
             "--output", str(tmp_summary.resolve())],
            capture_output=True, text=True, timeout=120, env=env,
        )
        if result.returncode != 0:
            print(f"ERROR: summarize failed (exit {result.returncode}): {result.stderr}")
            return
        print("OK: summary.json generated")

        # Inject MD executive summary into summary JSON
        md_summary = extract_md_executive_summary(md_path)
        if md_summary:
            summary = json.loads(tmp_summary.read_text())
            summary["executive_summary"] = md_summary
            tmp_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

        # Step 6b: Render PDF
        print("Running render-pdf...")
        result = subprocess.run(
            [str(PYTHON), "-m", "climate_delivery", "render-pdf",
             "--summary", str(tmp_summary.resolve()),
             "--output", str(tmp_pdf.resolve())],
            capture_output=True, text=True, timeout=120, env=env,
        )
        if result.returncode != 0:
            print(f"ERROR: render-pdf failed (exit {result.returncode}): {result.stderr}")
            return

        shutil.copy2(tmp_pdf, pdf_path)
        print(f"OK: {pdf_path} ({pdf_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
