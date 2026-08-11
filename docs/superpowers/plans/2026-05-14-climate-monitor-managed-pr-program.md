# Climate Monitor Managed PR Program

> **Historical planning snapshot:** Scheduled GitHub Actions guidance below has
> been superseded and that workflow has been deleted. Hermes is the sole report
> generator; see
> `docs/weekly-cadence.md` for the current rolling-PR workflow.

## Final User Outcome

1. Monitor every website listed in the Excel source file when a URL is available. Detect page/file changes, keep only climate-related items, and skip sites with no relevant changes.
2. Search for climate-related research and reports published within the last 30 days. Deduplicate results. Summarize items that are actuarial, insurance, risk, supervision, disclosure, solvency, pricing, reserving, or modeling relevant.
3. Produce a recurring English summary as `sources/climate-monitor-YYYY-MM-DD.md`, sync it into `wiki/`, and publish the result through GitHub.

## PR1: Automated Monitor Core

**Branch:** `codex/climate-monitor-pr1-automation`

**Goal:** Build the first end-to-end automated path in `climate_monitor_wiki`.

**Scope:**
- Convert the Excel source list into `monitoring/supranational_sources.yaml`.
- Add `monitoring/run_config.yaml` with climate and actuarial filters.
- Add `climate_monitor/` modules for config loading, live website collection through `web_listening`, research search, dedupe, report rendering, and orchestration.
- Add `scripts/run_climate_monitor.py`.
- Add dry-run fixtures and unit tests.
- Add GitHub Actions workflow for manual/scheduled runs and PR creation.
- Add documentation.

**Real Website Validation:**
- Run a live smoke against at least 5 real Excel sites: IAIS, ISSB, IPCC, OECD, WRI.
- Confirm the crawler reaches pages without secrets.
- Confirm the filter keeps climate-related links/pages and skips non-climate noise.
- Confirm failures are warnings, not whole-run crashes.

**Acceptance:**
- `python -m pytest`
- `node --check showcase/app.js`
- dry-run fixture creates one source report and synced wiki page.
- live smoke produces either climate items or explicit no-relevant-change/warning output.

## PR2: Reviewed Site Scope Bootstrap

**Branch:** `codex/climate-monitor-pr2-site-scopes`

**Goal:** Improve recall by adding reviewed monitoring scopes for all 34 URL-bearing organizations.

**Scope:**
- Add `monitoring/site_scopes/*.yaml` for reviewed seed URLs and preferred sections.
- Use `web_listening discover/classify` output where practical.
- Prefer publications, research, news, climate, sustainability, disclosure, supervision, and insurance/risk pages.
- Extend the adapter to use scoped seed URLs instead of only homepages.
- Add tests for scope loading and source-to-scope matching.

**Real Website Validation:**
- Run scoped smoke on all high-priority sites and at least 5 normal-priority sites.
- Manually inspect sampled outputs and remove irrelevant scopes.

**Acceptance:**
- PR1 tests still pass.
- New scope tests pass.
- Live scoped smoke has better climate/research signal than homepage-only mode.

## PR3: Document And Report Pipeline

**Branch:** `codex/climate-monitor-pr3-documents`

**Goal:** Treat PDF/DOCX/report files as first-class monitor items.

**Scope:**
- Download changed/new climate-related document links through `web_listening`.
- Add a document metadata manifest under generated state.
- Convert small text-readable documents or extracted snippets to summaries when possible.
- Leave hooks for `doc_to_md`, but do not require it for all runs.
- Add report sections separating website pages, files, and research reports.
- Add tests with fixture PDF/report metadata.

**Real Website Validation:**
- Use real public report links from IPCC, IAIS/ISSB/OECD/WRI when available.
- Confirm URL, title, source, date if available, and climate/actuarial relevance are correct.

**Acceptance:**
- PR1/PR2 tests still pass.
- Document fixture tests pass.
- Live document smoke includes only climate-related files.

## PR4: ai_interface Control Surface

**Branch:** `codex/climate-monitor-pr4-ai-interface`

**Goal:** Expose the monitor as an inspectable workflow in `ai_interface` without making it required for scheduled runs.

**Scope:**
- Add or update `ai_interface` skill/adapter metadata for the climate monitor workflow.
- Surface run inputs, outputs, artifacts, warnings, and generated report links.
- Keep GitHub Actions as the production scheduler.
- Add tests in `ai_interface` for manifest/adapter readiness and fixture rendering.
- Document how the control surface relates to the scheduled workflow.

**Real Website Validation:**
- Use PR1/PR2/PR3 generated artifacts as fixture inputs.
- Confirm displayed artifact URLs and summaries match generated reports.

**Acceptance:**
- `ai_interface` focused tests/build pass.
- Scheduled workflow can still run without `ai_interface`.

## Program Rules

- Each PR starts from latest `origin/main`.
- Each PR uses a fresh branch and a worker subagent implementation pass.
- Every PR gets spec review, code quality review, local verification, GitHub PR creation, checks/review triage, and merge before the next PR starts.
- Valid remote comments are fixed; speculative or unsafe comments are documented and skipped.
- Stop only after PR1-PR4 are complete, merged, or a concrete blocker is reported.
