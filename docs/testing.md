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
- `brief` vs `detailed` answer-mode behavior
- rolling date-window daily summaries such as `past 7 days`
- `/api/config` payload fields required by graph/dataview
- showcase root HTML containing both `Chat` and `Obsidian` workspaces

## Manual QA

### Chat

1. Start `uvicorn api_server:app --host 0.0.0.0 --port 8501`.
2. Open `/`.
3. Switch between `Brief` and `Detailed` and ask the same question in both modes.
4. Confirm the detailed answer is meaningfully richer.
5. Expand the `Evidence` drawer inside the assistant message.
6. Click a `wiki/` source card and confirm the app switches to the `Obsidian` tab with that note selected.
7. Click a `sources/` source card and confirm the raw report opens.
8. Ask `Summarize the past 7 days of reports` and confirm the answer explicitly covers each date in the window, including days with no source report.

### Obsidian Workspace

1. Open the `Obsidian` tab.
2. Confirm `Graph View` renders nodes and link lines.
3. Click a graph node and verify:
   - the Dataview row becomes selected
   - the detail panel updates
   - the chat header shows the active note badge
4. Search in the Dataview box and confirm the table filters in place.
5. Click `Use in chat`, switch back to `Chat`, and ask a question about the selected note in `Detailed` mode.

### Offline Mode

1. Remove or unset `OPENAI_API_KEY`.
2. Restart the server.
3. Confirm the status pill shows `Offline demo`.
4. Ask a question in `Detailed` mode and confirm the response still includes cited wiki + raw-source evidence.

## Gaps Worth Filling Later

- browser-level interaction tests for graph selection and source-card routing
- visual regression coverage for the two-tab workspace
- dedicated frontend unit tests if the showcase grows beyond a static JS surface
