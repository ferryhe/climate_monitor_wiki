# Source Update SOP

Use this workflow whenever `sources/` changes so the app, Dataview, and chat all stay in sync.

## Manual source-edit flow

1. Add or update the raw markdown file in `sources/`.
2. Run `python scripts/sync_source_wiki.py --cadence weekly` to regenerate the
   matching `wiki/climate-monitor-YYYY-MM-DD.md` pages and rebuild
   `wiki/index.md`.
3. Optionally append an entry to `wiki/log.md` and refresh any date/count text in `README.md`.
4. Reload the running API so the in-memory corpus picks up the filesystem change.
5. Run a smoke test against `/api/config` and `/api/chat`.
6. Run the fuller regression checks when the change is more than a simple append.

Validate that the filename date and internal `Report Date` agree before syncing.
Weekly mode renders only dates backed by real source files; it does not create
daily gap placeholders.

Manual source edits use the steps above on a feature branch and go through
normal review.

## Automated weekly flow

Hermes is the only scheduled production generator:

1. The monitor writes a Monday-dated report to the authoritative external
   report directory.
2. `scripts/weekly_wiki_refresh.sh` acquires a host lock and calls
   `scripts/publish_weekly_reports.py`.
3. The publisher clones the latest `origin/main` into a temporary directory,
   imports only missing validated reports, regenerates the weekly wiki, and
   runs the full test suite and static checks.
4. It updates only `codex/hermes-weekly-monitor` and creates or reuses its PR.
5. A human reviews and merges the PR. A separate server deployment then updates
   the clean production checkout and reloads the app.

The automated publisher never writes, commits, or syncs inside the production
checkout. It never reads `.env`, reloads the API, or restarts Docker. There is
no GitHub Actions report generator. Emergency manual generation runs only on
the controlled server through the same monitor and publisher.

## Why Reload Is Required

`api_server.py` creates one `AgenticWikiResponder` at startup, and the knowledge base loads `wiki/` and `sources/` into memory. Updating files on disk does not refresh chat retrieval, Dataview metadata, concept indexing, or `latest` date handling until you call `/api/reload` or restart the server.

## Quick Command

From the repository root:

```bash
REPORT_DATE="<new Monday, YYYY-MM-DD>"
python scripts/sync_source_wiki.py --cadence weekly
python scripts/reload_and_smoke_test.py --date "$REPORT_DATE"
```

If your API is not on the default local URL:

```bash
REPORT_DATE="<new Monday, YYYY-MM-DD>"
python scripts/sync_source_wiki.py --cadence weekly
python scripts/reload_and_smoke_test.py \
  --base-url http://localhost:8501 \
  --date "$REPORT_DATE"
```

If `/api/reload` is protected:

```bash
REPORT_DATE="<new Monday, YYYY-MM-DD>"
python scripts/sync_source_wiki.py --cadence weekly
RELOAD_TOKEN=your-token \
  python scripts/reload_and_smoke_test.py --date "$REPORT_DATE"
```

## Full Validation

```bash
python -m pytest
node --check showcase/app.js
```

## Expected Outcome

- `/api/config` includes `wiki/climate-monitor-YYYY-MM-DD.md`
- that report page points to `sources/climate-monitor-YYYY-MM-DD.md`
- `/api/chat` returns a non-empty answer with evidence
- the web workspace and Obsidian surface can see the new report page after reload
