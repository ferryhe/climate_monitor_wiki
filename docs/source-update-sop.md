# Source Update SOP

Use this workflow whenever `sources/` changes so the app, Dataview, and chat all stay in sync.

## Standard Flow

1. Add or update the raw markdown file in `sources/`.
2. Run `python scripts/sync_source_wiki.py` to regenerate all `wiki/climate-monitor-YYYY-MM-DD.md` pages and rebuild `wiki/index.md`.
3. Optionally append an entry to `wiki/log.md` and refresh any date/count text in `README.md`.
4. Reload the running API so the in-memory corpus picks up the filesystem change.
5. Run a smoke test against `/api/config` and `/api/chat`.
6. Run the fuller regression checks when the change is more than a simple append.

The sync script treats the source filename as the canonical day, so it can still regenerate the matching wiki page even when a source file's internal `Report Date` line is stale or malformed. It also keeps no-report placeholder pages for missing days inside the known date range so executive summaries can still surface coverage gaps cleanly.

## Why Reload Is Required

`api_server.py` creates one `AgenticWikiResponder` at startup, and the knowledge base loads `wiki/` and `sources/` into memory. Updating files on disk does not refresh chat retrieval, Dataview metadata, concept indexing, or `latest` date handling until you call `/api/reload` or restart the server.

## Quick Command

From the repository root:

```bash
python scripts/sync_source_wiki.py
python scripts/reload_and_smoke_test.py --date 2026-04-25
```

If your API is not on the default local URL:

```bash
python scripts/sync_source_wiki.py
python scripts/reload_and_smoke_test.py --base-url http://localhost:8501 --date 2026-04-25
```

If `/api/reload` is protected:

```bash
python scripts/sync_source_wiki.py
RELOAD_TOKEN=your-token python scripts/reload_and_smoke_test.py --date 2026-04-25
```

## Full Validation

```bash
python -m pytest
node --check showcase/app.js
```

## Expected Outcome

- `/api/config` includes `wiki/climate-monitor-YYYY-MM-DD.md`
- that daily page points to `sources/climate-monitor-YYYY-MM-DD.md`
- `/api/chat` returns a non-empty answer with evidence
- the web workspace and Obsidian surface can see the new daily page after reload
