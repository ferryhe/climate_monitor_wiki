# Source Update SOP

Use this workflow whenever `sources/` changes so the app, Dataview, and chat all stay in sync.

## Standard Flow

1. Add or update the raw markdown file in `sources/`.
2. If the change introduces a new date, create the matching `wiki/climate-monitor-YYYY-MM-DD.md` summary page.
3. Update `wiki/index.md` so the new date appears in the daily report table.
4. Optionally append an entry to `wiki/log.md` and refresh any date/count text in `README.md`.
5. Reload the running API so the in-memory corpus picks up the filesystem change.
6. Run a smoke test against `/api/config` and `/api/chat`.
7. Run the fuller regression checks when the change is more than a simple append.

## Why Reload Is Required

`api_server.py` creates one `AgenticWikiResponder` at startup, and the knowledge base loads `wiki/` and `sources/` into memory. Updating files on disk does not refresh chat retrieval, Dataview metadata, concept indexing, or `latest` date handling until you call `/api/reload` or restart the server.

## Quick Command

From the repository root:

```bash
python scripts/reload_and_smoke_test.py --date 2026-04-21
```

If your API is not on the default local URL:

```bash
python scripts/reload_and_smoke_test.py --base-url http://localhost:8501 --date 2026-04-21
```

If `/api/reload` is protected:

```bash
RELOAD_TOKEN=your-token python scripts/reload_and_smoke_test.py --date 2026-04-21
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
