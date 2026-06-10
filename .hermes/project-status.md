# climate_monitor_wiki Project Status

Last updated: 2026-06-10 08:54:37 EDT

## Identity

- Project: climate_monitor_wiki
- Worker slug: climate-wiki
- Repo path: /home/ec2-user/work/climate_monitor_wiki
- Remote: git@github.com:ferryhe/climate_monitor_wiki.git
- Current branch: fix/research-search-date-stability

## Current State

- Dirty at update: yes; scoped fix branch has local edits not yet committed.
- Active objective: fix recurring GitHub Actions `Climate Monitor` failure on `main` commit `ab78609`.
- Root cause: `test_search_recent_research_uses_injected_openai_client_when_key_is_set` relied on real `date.today()` while fixture data used `published: 2026-05-01` and `research_lookback_days=30`; once CI date moved beyond the lookback window, the mocked OpenAI result was filtered out and `items[0]` raised `IndexError`.
- Fix in progress: pass a fixed `today=date(2026, 5, 14)` in the injected-client test and use the same imported `date` helper in the lookback-window test.

## Last Verification

- 2026-06-10 EDT on `fix/research-search-date-stability`:
  - `python -m pytest tests/test_climate_monitor_research_search.py::test_search_recent_research_uses_injected_openai_client_when_key_is_set -vv` passed.
  - `python -m pytest tests` passed: 57 passed.
  - `node --check showcase/app.js` passed.
  - `git diff --check` passed.

## Next Safe Action

- Run the required pre-PR Codex review gate for the current diff.
- If accepted, commit, push, and open a narrow PR against `main`.
