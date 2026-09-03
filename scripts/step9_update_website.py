#!/usr/bin/env python3
"""Step 9: Update website (wiki pages + registry reload)."""
import argparse
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

REPORTS = Path("/home/ubuntu/climate_monitor_wiki/data/reports")
SOURCES = Path("/home/ubuntu/climate_monitor_wiki/sources")
PYTHON = Path("/home/ubuntu/climate_monitor_wiki/.venv/bin/python")
REPO = "/home/ubuntu/climate_monitor_wiki"
RELOAD_TOKEN = "b49ca3d610ed7f41b6e24ecad794c28f21d9e6f5f06b965f"


def last_monday():
    """Get the most recent Monday."""
    today = date.today()
    if today.weekday() == 0:
        return today
    return today - timedelta(days=today.weekday())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=last_monday().isoformat())
    args = parser.parse_args()

    # Step 9a: Copy latest MD to sources/ for wiki generation
    md_path = REPORTS / f"climate-monitor-{args.date}.md"
    if not md_path.exists():
        print(f"ERROR: markdown not found: {md_path}")
        return

    sources_dir = SOURCES
    sources_dir.mkdir(parents=True, exist_ok=True)
    dest = sources_dir / f"climate-monitor-{args.date}.md"
    shutil.copy2(md_path, dest)
    print(f"Copied MD to sources/")

    # Step 9b: Regenerate wiki pages
    try:
        result = subprocess.run(
            [str(PYTHON), "scripts/sync_source_wiki.py", "--cadence", "weekly"],
            capture_output=True, text=True, timeout=120, cwd=REPO
        )
        if result.returncode != 0:
            print(f"WARNING: wiki sync failed: {result.stderr}")
        else:
            print("OK: wiki pages regenerated")
    except Exception as e:
        print(f"WARNING: wiki sync error: {e}")

    # Step 9c: Reload registry in container via API
    try:
        reload_script_content = (
            "import urllib.request, json\n"
            "req = urllib.request.Request(\n"
            "    'http://localhost:8501/api/reload',\n"
            "    data=json.dumps({}).encode(),\n"
            "    method='POST',\n"
            "    headers={'Content-Type': 'application/json', 'X-Reload-Token': '" + RELOAD_TOKEN + "'}"
            ")\n"
            "with urllib.request.urlopen(req, timeout=30) as resp:\n"
            "    print('Reload:', resp.status)\n"
        )
        result = subprocess.run(
            ["sudo", "docker", "exec", "-i", "climate-wiki-app", "python3"],
            input=reload_script_content,
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            print(f"WARNING: container reload failed: {result.stderr}")
        else:
            print("OK: container registry reloaded")
    except Exception as e:
        print(f"WARNING: container reload error: {e}")


if __name__ == "__main__":
    main()
