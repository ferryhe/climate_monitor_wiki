# Testing

## Automated Checks

Run these from the repository root:

```bash
source .venv/bin/activate
python -m pytest
node --check showcase/app.js
```

Current automated coverage includes:

- wiki loading and chunk generation
- raw `sources/` ingestion into retrieval
- `contextPath` source prioritization
- `brief` vs `detailed` vs `executive` answer-mode behavior
- rolling date-window daily summaries such as `past 7 days`
- monthly/date-range prompts such as `this month` and explicit ISO date ranges
- `/api/config` payload fields required by graph/dataview, prompt starters, and precomputed graph payloads
- showcase root HTML containing both `Chat` and `Obsidian` workspaces

## Manual QA

### Chat

1. Start `uvicorn api_server:app --host 0.0.0.0 --port 8501`.
2. Open `/`.
3. Switch between `Brief`, `Detailed`, and `Report` and ask the same question in all three modes.
4. Confirm the detailed answer is meaningfully richer than `Brief`, and that `Report` uses sectioned report-style output with clustered themes instead of only a day-by-day recap.
5. Expand the `Evidence` drawer inside the assistant message.
6. Click a `wiki/` source card and confirm the app switches to the `Obsidian` tab with that note selected.
7. Click a `sources/` source card and confirm the raw report opens.
8. Ask `Summarize the past 7 days of reports` and confirm the answer explicitly covers each date in the window, including days with no source report.
9. Ask `Give me a report for this month` and confirm the answer includes `Executive Summary`, `Major Themes`, and `Date Coverage`.

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
- visual regression coverage for the two-tab workspace
- dedicated frontend unit tests if the showcase grows beyond a static JS surface
