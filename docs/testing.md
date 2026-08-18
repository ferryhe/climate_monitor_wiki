# Testing

## Automated Checks

Run these from the repository root:

```bash
source .venv/bin/activate
python -m pytest
node --check showcase/app.js
```

The 2026-08-18 Registry-automation run collected 913 tests and completed with
899 passed and 14 environment-specific skips on Windows.

Current automated coverage includes:

- wiki loading and chunk generation
- raw `sources/` ingestion into retrieval
- `contextPath` source prioritization
- `brief` vs `detailed` vs `executive` answer-mode behavior
- rolling weekly-report summaries such as `past 4 weeks`
- monthly/date-range prompts such as `this month` and explicit ISO date ranges
- `/api/config` payload fields required by graph/dataview, prompt starters, and precomputed graph payloads
- showcase root HTML containing `Historical Reports`, `Chat`, and `Obsidian` workspaces
- exact-date weekly Registry dry-run, candidate promotion, rollback/restore,
  DB-first metadata precedence, and the unscheduled 10:30 runner contract

## Manual QA

### Historical Reports

1. Open `/` and confirm `Historical Reports` is the default tab.
2. Select each available weekly report and confirm the narrative `Executive Summary`,
   `Monitoring Snapshot`, article list, and PDF download are present where an
   artifact exists.
3. Switch reports and confirm the detail pane follows the selected date.
4. Switch to `Article Database`, exercise search and publisher/pillar filters,
   and open an article detail.
5. Repeat at a mobile viewport and confirm list/detail navigation remains usable.

### Chat

1. Start `uvicorn api_server:app --host 0.0.0.0 --port 8501`.
2. Open `/`.
3. Switch between `Brief`, `Detailed`, and `Report` and ask the same question in all three modes.
4. Confirm the detailed answer is meaningfully richer than `Brief`, and that `Report` uses sectioned output with clustered themes plus report-by-report coverage.
5. Expand the `Evidence` drawer inside the assistant message.
6. Click a `wiki/` source card and confirm the app switches to the `Obsidian` tab with that note selected.
7. Click a `sources/` source card and confirm the raw report opens.
8. Ask `Summarize the past 4 weeks` and confirm the answer lists only real report dates in the window, without manufacturing missing daily updates.
9. Ask `Give me an executive report for the past 12 weeks` and confirm the answer includes `Executive Summary`, `Major Themes`, `Date Coverage`, and `Report-by-Report Coverage`.

### Obsidian Workspace

1. Open the `Obsidian` tab.
2. Confirm the page order is `Dataview + Note Detail` first, then `Graph View`.
3. Confirm `Graph View` renders nodes and link lines.
4. Switch from `Notes` to `Keywords` and confirm the keyword graph appears promptly with both note and keyword nodes.
5. Click a graph node and verify:
   - the Dataview row becomes selected
   - the detail panel updates
   - the chat header shows the active note badge
6. Search in the Dataview box and confirm the table filters in place.
7. Select a daily report note and click `Source`; confirm it opens the matching `sources/*.md` file on GitHub `main`.
8. Click `Use in chat`, switch back to `Chat`, and ask a question about the selected note in `Detailed` mode.

### Offline Mode

1. Remove or unset `OPENAI_API_KEY`.
2. Restart the server.
3. Confirm the status pill shows `Offline demo`.
4. Ask a question in `Detailed` mode and confirm the response still includes cited wiki + raw-source evidence.
5. Ask `Give me a report for this month` and confirm the response still returns the report sections in extractive form.

## Gaps Worth Filling Later

- browser-level interaction tests for graph selection and source-card routing
- visual regression coverage for the three-tab workspace
- dedicated frontend unit tests if the showcase grows beyond a static JS surface
