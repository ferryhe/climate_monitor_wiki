Run the weekly climate & actuarial monitoring report for the IAA CSC Supranational Organizations working group.

CONTEXT / ENVIRONMENT:
- web_listening is installed at /home/ubuntu/web_listening (Python 3.12 venv at .venv).
- 57 org sites registered & monitored (37 original supranational orgs + 20 tier-3 supplementary sources; 18 browser-mode for 403/SSL, 39 http). None skipped. Tier-3 orgs carry the `tier-3` tag (ids 38-57) so they are removable as a unit.
- Driver: /home/ubuntu/web_listening/weekly_driver.py (runs `check` on all sites, builds markdown report to data/reports/climate-monitor-YYYY-MM-DD.md, prints to stdout). The driver now supports --article-changes-json (Pillar A items with summaries) and --pillar-b-json (Pillar B items, optionally with summaries), and automatically FILTERS Pillar A to only climate/actuarial-relevant items (item-level keyword filter — non-relevant items like pure personnel/staffing announcements are dropped, and the report notes how many were filtered out).

STEP-BY-STEP (do ALL, in order):
0. PREVIOUS-REPORT / REGISTRY SYNCHRONIZATION PREFLIGHT (run BEFORE any collection/model/network step):
Set the scheduled Monday date and run the following read-only preflight. It proves the latest prior
producer report is present in the deployed app sources with a matching SHA-256, and that the Registry
and app source history are synchronized, before paying for a new collection.
  export MON_REPORT_DATE=<TODAY>
  python3 - <<'PY'
import os, sys, json, tempfile, hashlib, subprocess, re, shutil
from pathlib import Path
from datetime import date

MONDAY = os.environ["MON_REPORT_DATE"]
APP_REPO = os.environ.get("MON_APP", "/home/ubuntu/climate_monitor_wiki")
APP_PY = os.environ.get("MON_APP_PY", "/home/ubuntu/climate_monitor_wiki/.venv/bin/python")
REG_DB = os.environ.get("MON_REG", "/home/ubuntu/climate_monitor_data/registry/article-registry.sqlite3")
SRC = os.environ.get("MON_SRC", "/home/ubuntu/climate_monitor_wiki/sources")
PROD_REPORTS = os.environ.get("MON_PROD_REPORTS", "/home/ubuntu/web_listening/data/reports")

def fail(code):
    print("result_code=" + code)
    sys.exit(0)

# 1. latest prior producer report dated earlier than MONDAY
cands = []
for f in Path(PROD_REPORTS).glob("climate-monitor-*.md"):
    m = re.match(r"climate-monitor-(\d{4}-\d{2}-\d{2})\.md", f.name)
    if not m:
        continue
    try:
        d = date.fromisoformat(m.group(1))
    except Exception:
        continue
    if d.isoformat() < MONDAY:
        cands.append((d, f))
if not cands:
    fail("registry_history_lagging:no_prior_report")
cands.sort()
prior_date, prior = cands[-1]
# 2. identical filename present in app sources
src_file = Path(SRC) / prior.name
if not src_file.exists():
    fail("registry_history_lagging:missing_source")
# 3. producer and app-source SHA-256 match exactly
if hashlib.sha256(prior.read_bytes()).hexdigest() != hashlib.sha256(src_file.read_bytes()).hexdigest():
    fail("registry_history_lagging:sha_mismatch")
# 4. zero-candidate selection proves Registry/source readability + synchronization
tmpd = tempfile.mkdtemp(prefix=".pf-")
os.chmod(tmpd, 0o700)
inp = Path(tmpd) / "preflight-input.json"
inp.write_text(json.dumps({"schema_version": "registry-selection-input.v1",
                           "report_date": MONDAY, "candidates": []}))
os.chmod(inp, 0o600)
try:
    r = subprocess.run([APP_PY, "-m", "climate_registry", "plan-selection",
                        "--database", REG_DB, "--source-dir", SRC, "--input", str(inp)],
                       capture_output=True, text=True, cwd=APP_REPO)
    if r.returncode != 0:
        fail("registry_history_lagging:cli_error")
    plan = json.loads(r.stdout.strip())
    if plan.get("schema_version") != "registry-selection-plan.v1" or plan.get("report_date") != MONDAY:
        fail("registry_history_lagging:schema")
    if plan.get("counts", {}).get("total") != 0 or len(plan.get("decisions", [])) != 0:
        fail("registry_history_lagging:nonzero_decisions")
    print("result_code=history_ok")
finally:
    shutil.rmtree(tmpd, ignore_errors=True)
  PY
  On result_code=registry_history_lagging: STOP. Do NOT collect, do NOT call the model/network, do NOT
  build or deliver a report, do NOT fall back to unfiltered candidates. Mark this attempt failed.
  On result_code=history_ok: continue to step 1.

1. `cd /home/ubuntu/web_listening && source .venv/bin/activate`
2. Live web_search for climate-change info RELEVANT TO ACTUARIES in the LAST 3 MONTHS. Queries: "climate change actuarial risk insurance disclosure 2026", "IFRS S2 ISSB climate disclosure actuary 2026", "parametric insurance climate adaptation 2026", "Swiss Re Munich Re nat cat 2026 climate loss actuarial", "climate risk scenario actuarial 2026". Collect 12-18 items (title+url+source). DEDUPLICATE by URL and near-identical content.
3. FOR EACH Pillar B item, write a short 1-2 line SUMMARY (what the item is about and why it matters to actuaries/climate risk). Save as /home/ubuntu/web_listening/pillar_b_<TODAY>.json in shape: [{"title","url","source":"web","summary":"..."},...]. (summary-first rendering is required by the report format.)
4. Pillar A: run the per-org article tracker (or use stored state) to get new articles. KEEP ONLY those relevant to climate change / actuarial risk. For each kept item, write a 1-2 line SUMMARY. Save as /home/ubuntu/web_listening/article_changes_<TODAY>.json in shape: [{"org":"...","items":[{"url","title","summary"}]}]. Non-relevant items (e.g. personnel appointments, general org news) must be EXCLUDED here.
4b. REGISTRY SELECTION GATE (mandatory, deterministic, fail-closed):
Before building the report, gate the already-created candidate JSON through the
deployed PR #44 Registry selection CLI. Do NOT recollect, call the network, or
call a model at this stage.
  a. Reuse the exact files from steps 3-4:
       article_changes_<TODAY>.json   (Pillar A candidates)
       pillar_b_<TODAY>.json          (Pillar B candidates)
     Keep these original files untouched for same-week retry.
  b. Assign deterministic opaque candidate IDs:
       Pillar A: a-0001, a-0002, ... in file order
       Pillar B: b-0001, b-0002, ... in file order
     IDs contain only lowercase ASCII and digits; they never embed URL/title/path.
  c. Build a strict input document (mode 0600) in a mode-0700 temp dir OUTSIDE
     both repos:
       {"schema_version":"registry-selection-input.v1",
        "report_date":"<Monday YYYY-MM-DD>",
        "candidates":[
          {"candidate_id":"a-0001","pillar":"A","title":<t>,"url":<u>,"summary":<s>},
          {"candidate_id":"b-0001","pillar":"B","title":<t>,"url":<u>,"summary":<s>}, ...]}
     Use only the contract-required fields; do not invent summary/URL.
  d. Invoke read-only from the app repo:
       cd /home/ubuntu/climate_monitor_wiki
       .venv/bin/python -m climate_registry plan-selection \
         --database /home/ubuntu/climate_monitor_data/registry/article-registry.sqlite3 \
         --source-dir /home/ubuntu/climate_monitor_wiki/sources \
         --input <temp-input>
  e. Require ALL of: exit 0; exactly one compact JSON line; schema_version
     "registry-selection-plan.v1"; matching report_date; exactly one decision per
     input candidate_id; no duplicate/unknown/missing IDs; pillar matches input;
     disposition in {selected,rejected}; recognized reason; counts consistent.
  f. On ANY CLI/output/contract failure (nonzero exit, empty/malformed output,
     wrong schema or report_date, missing/dup/unknown decision ID, pillar
     mismatch, inconsistent counts): DO NOT build or deliver a report, DO NOT
     fall back to unfiltered candidates. Retain the original A/B files and emit
     only the safe status "registry_selection_failed"; mark this attempt failed.
  g. On a VALID plan:
       - Keep ONLY "selected" candidates; preserve original order within each pillar.
       - Never move B into A or A into B; never alter title/summary/url.
       - Write filtered files:
           article_changes_filtered_<TODAY>.json   (selected Pillar A only)
           pillar_b_filtered_<TODAY>.json          (selected Pillar B only)
       - If selected count is 0: treat as a valid no_change; do NOT generate or
         deliver a report; retain original A/B files; allow the 09:00 Email and
         10:00 Publisher to observe that no Monday report exists.
  h. Use the filtered files (not the raw ones) in the driver step below.
  i. Remove all temp selection/input/output files in a finally block. Do NOT
     delete the original article_changes_<TODAY>.json or pillar_b_<TODAY>.json.
  j. Do NOT modify article_state.json from Registry decisions; it remains an
     observed/processed URL store only.

  g2. After plan-selection returns a valid plan, persist two files (mode 0600) required by the
  staging wrapper in step 5:
    - the exact plan JSON output you received -> /tmp/monitor-selection-plan.json
    - the exact registry-selection-input document you built -> /tmp/monitor-selection-input.json
  Do not print their contents.

5. Build and atomically install the report via the staging wrapper (replaces the direct driver call).
This reuses the REAL weekly_driver.main() but redirects its output to a hidden staging directory
inside the production reports folder, validates the staged report with the deployed app parser, then
atomically installs it with os.replace. It never overwrites an existing final report.
  export MON_REPORT_DATE=<TODAY>
  export MON_PB_JSON=/home/ubuntu/web_listening/pillar_b_filtered_<TODAY>.json
  export MON_AC_JSON=/home/ubuntu/web_listening/article_changes_filtered_<TODAY>.json
  export MON_PLAN_JSON=/tmp/monitor-selection-plan.json
  export MON_CAND_JSON=/tmp/monitor-selection-input.json
  python3 - <<'PY'
import os, sys, json, tempfile, hashlib, subprocess, shutil
from pathlib import Path

MONDAY = os.environ["MON_REPORT_DATE"]
PB = os.environ["MON_PB_JSON"]
AC = os.environ["MON_AC_JSON"]
PLAN = os.environ["MON_PLAN_JSON"]
CAND = os.environ["MON_CAND_JSON"]
APP_REPO = os.environ.get("MON_APP", "/home/ubuntu/climate_monitor_wiki")
APP_PY = os.environ.get("MON_APP_PY", "/home/ubuntu/climate_monitor_wiki/.venv/bin/python")
WL = os.environ.get("MON_WL", "/home/ubuntu/web_listening")

sys.path.insert(0, WL)
import weekly_driver

final_reports = weekly_driver.REPORTS
final_report = final_reports / ("climate-monitor-" + MONDAY + ".md")

# 5. fail closed if final already exists; never overwrite
if final_report.exists():
    print("result_code=report_already_exists")
    sys.exit(0)

# 6. staging dir INSIDE production reports dir => same filesystem
staging = tempfile.mkdtemp(prefix=".stage-", dir=str(final_reports))
os.chmod(staging, 0o700)
old_reports = weekly_driver.REPORTS
old_argv = sys.argv[:]
val_file = None
try:
    # 7. redirect real driver output to staging only
    weekly_driver.REPORTS = Path(staging)
    # 7b. main() unconditionally calls collect_changes() which spawns the
    # web-listening CLI (network). We already supply filtered candidates, so
    # neutralize that one network sub-process in-process (no source edit).
    weekly_driver.collect_changes = lambda since: ("", None)
    # 8. invoke the REAL weekly_driver.main() with exactly the filtered args
    sys.argv = ["weekly_driver.py", "--pillar-b-json", PB,
                "--article-changes-json", AC, "--deliver-only", "--date", MONDAY]
    weekly_driver.main()
    # 9. exactly one canonical Monday file in staging
    staged = [p for p in Path(staging).iterdir() if p.is_file()]
    if len(staged) != 1 or staged[0].name != final_report.name:
        print("result_code=staging_unexpected_files")
        sys.exit(2)
    staged_report = staged[0]
    # 10. validate with the REAL deployed app parser + selected/rejected coverage
    val_src = """import sys, json
sys.path.insert(0, "/home/ubuntu/climate_monitor_wiki")
from pathlib import Path
from climate_registry.selection import parse_strict_weekly_report, canonical_url, canonical_title
staged=Path(sys.argv[1]); plan=json.load(open(sys.argv[2])); cands={c["candidate_id"]:c for c in json.load(open(sys.argv[3]))["candidates"]}
rep=parse_strict_weekly_report(staged)
dec={d["candidate_id"]:d for d in plan["decisions"]}
selected={cid:cands[cid] for cid,d in dec.items() if d["disposition"]=="selected"}
parsed=[(a.pillar, canonical_title(a.title), canonical_url(a.url)) for a in rep.articles]
sel_keys={(c["pillar"], canonical_title(c["title"]), canonical_url(c["url"])) for c in selected.values()}
if len(parsed)!=len(sel_keys): sys.exit(3)
if any(k not in sel_keys for k in parsed): sys.exit(4)
if len(parsed)!=len(selected): sys.exit(5)
a_urls={canonical_url(c["url"]) for c in selected.values() if c["pillar"]=="A"}
a_titles={canonical_title(c["title"]) for c in selected.values() if c["pillar"]=="A"}
for c in selected.values():
    if c["pillar"]=="B":
        if canonical_url(c["url"]) in a_urls or canonical_title(c["title"]) in a_titles: sys.exit(6)
print("VALID")"""
    val_file = Path(staging) / ".validate.py"
    val_file.write_text(val_src)
    os.chmod(val_file, 0o600)
    v = subprocess.run([APP_PY, str(val_file), str(staged_report), PLAN, CAND],
                       capture_output=True, text=True, cwd=APP_REPO)
    if v.returncode != 0 or "VALID" not in v.stdout:
        print("result_code=validation_failed", v.returncode, (v.stderr or v.stdout)[:200])
        sys.exit(7)
    # 11. durability before install
    data = staged_report.read_bytes()
    h = hashlib.sha256(data).hexdigest()
    fd = os.open(str(staged_report), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if hashlib.sha256(staged_report.read_bytes()).hexdigest() != h:
        print("result_code=staged_changed")
        sys.exit(8)
    # 12. recheck immediately before install
    if final_report.exists():
        print("result_code=report_already_exists")
        sys.exit(9)
    if Path(staging).resolve().parent != final_reports.resolve():
        print("result_code=fs_mismatch")
        sys.exit(10)
    # 13. atomic install
    os.replace(str(staged_report), str(final_report))
    # 14. durability after install
    assert hashlib.sha256(final_report.read_bytes()).hexdigest() == h
    try:
        dfd = os.open(str(final_reports), os.O_DIRECTORY)
        os.fsync(dfd)
        os.close(dfd)
    except Exception:
        pass
    print("result_code=ok", h)
finally:
    weekly_driver.REPORTS = old_reports
    sys.argv = old_argv
    if val_file is not None and val_file.exists():
        val_file.unlink()
    shutil.rmtree(staging, ignore_errors=True)
  PY
  If the wrapper prints result_code=report_already_exists, stop (do not overwrite).
  If it prints result_code=validation_failed or any other failure, do NOT deliver; mark attempt failed.
  On result_code=ok, the report is installed at data/reports/climate-monitor-<TODAY>.md.

6. If any site FAILED `check`, fix: switch to browser mode via sqlite UPDATE sites SET fetch_mode='browser' WHERE name=? then re-check. Persist until all pass.
7. DELIVER to Feishu via lark-cli bot message. Split into <=3 messages to stay under post-message length limits, table-free. Each item in the report now leads with a summary line then the link — preserve that structure in the delivered messages:
   - Msg 1: Executive summary + Pillar A relevant items.
   - Msg 2: Pillar B items (summary + link) part 1.
   - Msg 3: Pillar B items (summary + link) part 2 + a few original links + note that full report is at data/reports/climate-monitor-<TODAY>.md.
   Ensure PATH includes ~/.local/bin.
8. Also save report to data/reports/ (driver does this).
9. ALSO push the full markdown report to the IAA_AITF Discord channel so the report body lands there. Use the dedicated pusher (auto-chunks <=1950 chars):
   `python3 /home/ubuntu/web_listening/scripts/push_report_discord.py data/reports/climate-monitor-<TODAY>.md`
   It reads DISCORD_BOT_TOKEN from /home/ubuntu/.hermes/.env. This must run AFTER the report file exists. Confirm the returned message ids.

OUTPUT (your final response): confirm sites checked/ok/failed, # Pillar A relevant changes (and # filtered out), # Pillar B after dedup, Feishu delivery message_ids, AND Discord delivery message_ids. Do NOT also try to create a Feishu doc (scope unavailable).

NOTE: Budget ~25 min; run checks sequentially; tolerate slow browser-mode sites.