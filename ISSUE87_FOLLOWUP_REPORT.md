# Issue #87 follow-up handoff — Refs #87

Issue #87 stays OPEN. This is an implementation handoff, not production completion.
The manager owns commit/push/PR/review/merge. No production email, publisher push,
Registry write, cron change, Render environment change, or host timezone change
was performed. A fresh read-only reviewer must review the final diff; a manager
inline review or a timed-out delegation does not satisfy that gate.

## Implementation and remaining gates

The wrappers now fail closed and support read-only preflight. Production monitor
execution remains explicitly blocked with `live_acquisition_contract_unavailable`:
there is no demonstrated executable same-run #67 outcome → #92 evidence → #93
response entry in this wrapper. Supplying three unrelated JSON files cannot clear
that blocker. This is the fail-closed alternative explicitly permitted by the task.
The isolated acceptance harness exercises synthetic acquisition fixtures only.
It is not evidence that upstream production has migrated or a normal run occurred.

Public Render has no shared source for the local scheduler snapshot. The existing
503 `not_configured` is retained; no transport or unavailable mounted volume is
proposed. Missing jobs/config/runtime remain operator provisioning blockers.

The full-chain acceptance test exposed two additional consumer incompatibilities:
validated v2 reports lacked weekly Pillar sections/counts/source links, and the
publisher rejected distinct URLs with the same title. The v2-only renderer now
serializes validated counts and authored summaries in the existing weekly form.
Publisher selection follows URL identity only for a verified SHA-bound semantic
bundle; legacy title deduplication remains the default.

Exact command output and red/green logs are retained under
`/tmp/issue87-evidence/`. They must accompany manager review. CI requirements remain
`python-tests` 3.11/3.12, `repository-checks`, and `docker-build-smoke`.
A local environment failure is not a CI pass.

## Draft continuation prompt (manager copies; do not resume the old job)

You are the read-only normal-run observation lane for Issue #87.
`f64c1563ff95 remains paused`. Do not resume it from this prompt.

1. First verify that the follow-up PR containing `Refs #87` actually merged and
   read back the reviewed commit deployed in each relevant runtime. Confirm the
   real acquisition/configuration chain is executable. Do not infer deployment
   from PR #102 or from this report.
2. Read the actual Hermes inventory and the four jobs' commands, explicit paths,
   timezone, enabled state and schedules. Read each wrapper's preflight result.
   Check delivery config via its loader without printing recipients or secrets.
   Verify the real ledger/status/state/report/Registry paths and upstream runtime.
   If anything is missing, report the blocker and do NOT create or send anything.
3. Only observe a normal Monday whose four jobs were read back before Monday 16:00 CST. Hermes is Asia/Shanghai: 08/09/10/10:30 UTC maps to 16/17/18/18:30
   CST. The observation window starts after 18:30 CST, not 10:30 local. Do not
   change global timezone. If prerequisites missed that window, select the next
   eligible Monday; elapsed time is never evidence of provisioning.
4. Observe the real same-date acquisition outcome, canonical six-key counts,
   response/request identity, Markdown and sidecar SHA, PDF/manifest identity,
   and four configured recipients' successful delivery statuses (no addresses
   or secrets in the report). Then observe the rolling PR, separate human review,
   merge and deployment, followed by the explicitly enabled Registry runner and
   matching deployed-source/artifact/DB/API identities.
5. A failed, dry-run, disabled or `not_dispatched` Registry stage is pending and
   does not complete the chain. Do not auto-merge a publisher PR or trigger SMTP,
   capture, reload, DB promotion, scheduler creation or configuration changes.
6. Render's `/api/job-status` is honestly 503 `not_configured` while there is no
   shared source. Use actual scheduler readback, existing ledger/API evidence,
   and explicit pending fields. Do not invent a remote snapshot or ask for a
   volume that the operator cannot install.
7. Issue #87 stays OPEN until one normal Monday run is observed end to end.
   Only a separate closeout PR after that observation may contain `Closes #87`.
   The closeout requires a fresh read-only reviewer on the final tree, all four
   required CI checks and normal protected merge. A reviewer timeout leaves
   the gate pending; it never authorizes an inline-review substitution.

Return exact observed identities/timestamps, evidence locations, and pending
blockers. Missing provisioning is a blocker report, not authorization to create it.

## Draft rollback runbook (manager copies; do not execute)

The backup at `/root/.ops/backups/climate-monitor-87/20260907-005803` is evidence,
not a recovery tool. Select this exact evidence set; do not discover a latest
backup heuristically or restore scheduler storage files.

1. Read back the affected deployed commits and current job inventory. Preserve
   evidence of current state and identify the exact jobs/changes introduced by
   the rollout. Stop if ownership, local edits, or the intended target is unclear.
2. Code rollback uses a normal branch from current `origin/main`, `git revert`
   of the reviewed changes (with the correct mainline only for a merge commit),
   required tests/CI, a fresh read-only reviewer and a normal revert PR. Use
   `Refs #87`; never bypass protection, force-push or merge as administrator.
3. After that revert PR merges, an authorized deployment operator verifies the
   local checkout is clean and tracks main, fetches origin, and uses
   `git merge --ff-only origin/main`. Stop if clean/fast-forward checks fail.
   Verify the reviewed revert commit on the local deployment and Render's
   automatic main deployment, plus the relevant health/config/corpus checks.
4. Cron recovery uses only `hermes cron list/remove/update/create`. Read back
   live state first. Use the CLI's help for supported options. Remove only an
   exact confirmed rollout-created job; update an identified changed job; create
   a replacement only when its intended configuration and absence are verified
   and separately authorized. Read back state after each action. Preserve
   16/17/18/18:30 CST for the 08/09/10/10:30 UTC chain and the human Registry gate.
   Never copy a saved scheduler JSON file into live storage. Do not resume
   `f64c1563ff95` as a recovery shortcut.
5. Preserve historical reports, sources, seen state, delivery records, Registry
   DB and ledger. This application rollback does not rewrite those histories.
   Any separately necessary data recovery requires its existing reviewed exact
   restore procedure and authorization. Do not execute it from this runbook.
6. Render has no shared scheduler source. Verify honest 503 `not_configured`
   rather than configuring a nonexistent mount. Any environment change needs
   separate operator authorization; this code rollback performs none.
7. Record before/after commit and job readbacks, CI/deployment evidence and
   remaining blockers. Issue #87 stays OPEN. `Closes #87` belongs only in a
   separate closeout PR after a normal end-to-end Monday observation.

## Changed files and acceptance mapping

| Repo-relative path | Reason / AC |
|---|---|
| `scripts/hermes_job.py` | Shared read-only path/config/identity checks and gated dispatch; AC-1/3/10. |
| `scripts/hermes_job_monitor.sh` | Absolute entry, explicit interpreter, no automatic fixture fallback; AC-1/10. |
| `scripts/hermes_job_email.sh` | Delegate to verified public delivery CLI; AC-1/10. |
| `scripts/hermes_job_publisher.sh` | Validate explicit report/ledger paths and invoke absolute publisher; AC-10. |
| `scripts/hermes_job_registry.sh` | Delegate to explicit human/enable/write gates; AC-3/10. |
| `scripts/run_climate_monitor.py` | Honor the existing explicit loopback provider in weekly mode; AC-1/5/10. |
| `climate_monitor/weekly_monitor/driver.py` | Forward that provider to prevent dry-run default live acquisition; AC-1/5. |
| `scripts/weekly_registry_refresh.py` | Real dry-run exit and expected report SHA binding; AC-3/10. |
| `climate_delivery/cli.py` | Optional expected-report SHA at the public CLI boundary; AC-1/10. |
| `climate_delivery/pipeline.py` | Reject a changed report before artifact or SMTP work; AC-1/10. |
| `climate_monitor/orchestrator.py` | Opt validated v2 responses into weekly consumer serialization; AC-1/5. |
| `climate_monitor/report_writer.py` | Weekly Pillar sections, real validated counts and authored summary; preserve legacy default; AC-1/2/5. |
| `climate_monitor/semantic_bundle.py` | Verify the existing weekly source-link form as well as the legacy URL-field form; AC-5. |
| `climate_registry/selection.py` | Explicit duplicate-title policy option, preserving the legacy default; AC-5. |
| `scripts/publish_weekly_reports.py` | Permit distinct same-title URLs only with a verified semantic bundle; AC-5. |
| `scripts/dryrun_full_pipeline.py` | Isolated full-chain scenario and before/after state/ref fingerprints; AC-5. |
| `scripts/dryrun_isolated_pipeline.py` | Label the old check as contract-only and print PASS only after checks; AC-5/6. |
| `tests/test_hermes_job_wrappers.py` | Wrapper subprocess boundaries, identity guards and consumer regressions; AC-1/2/5/7/10. |
| `tests/dryrun_isolated_pipeline_full.py` | Full-chain and snapshot success/failure tests, included in ordinary discovery; AC-5/7. |
| `tests/test_scheduler_status.py` | Pin the observer clock in the existing thread test; verify isolated Registry exit evidence at 10:30 UTC; AC-3/7. |
| `monitoring/jobs/weekly-climate-monitor-08h/manifest.json` | Explicit UTC to Asia/Shanghai mapping; historical capture remains intact; AC-3/10. |
| `monitoring/jobs/weekly-climate-monitor-08h/driver/hermes-entrypoint.md` | Absolute wrapper/preflight template and local cron mapping; AC-6/10. |
| `docs/job-status.md` | Honest local-only evidence, optional Registry slot and remote 503; AC-3/6. |
| `docs/weekly-cadence.md` | Correct timezone and unprovisioned-state caveat; AC-6/10. |
| `PIPELINE_CONFIG.md` | Correct intended schedule, CLI and explicit dry-run semantics; AC-6/10. |
| `PIPELINE_REFERENCE.md` | Canonical wrapper configuration and identity/gate contracts; AC-6/10. |
| `README.md` | Align intended topology and current provisioning limitations; AC-6/12. |
| `ISSUE87_FOLLOWUP_REPORT.md` | Evidence handoff and replacement continuation/rollback drafts; AC-6/8/12. |

## Red/green evidence and sibling inspection

Full logs are in `/tmp/issue87-evidence`; the final affected-code regression gate
is `final-focused.txt` (366 passed). Tests added as supplementary checks after
implementation are not misrepresented as pre-fix red runs. In particular, the
positive arbitrary-CWD rehearsal supplements the initial monitor fail-closed red
rather than constituting an independently captured pre-fix F11 red. F6 is a real
manager review gate, not something a documentation test can certify.

| Finding | Red evidence / exact failure | Fixed boundary and green evidence |
|---|---|---|
| F1 | `test_f1_monitor_missing_real_evidence_fails_before_status`; `red-wrappers.txt`: expected `missing_AUTHORING_RESPONSE`, got empty stdout and scheduler validation error. Four fixture-flag cases also failed. | `path_env`, `monitor_command`, `main`; no fixture fallback, production fixture paths rejected. Same tests pass in `final-focused.txt`. |
| F2 | `test_f2_email_plan_uses_public_cli_and_checks_identity`; `red-email-registry.txt`: helper import failed before implementation. `test_email_changed_report_sha_fails_before_artifacts` and `test_f2_pdf_tamper_blocks_email_dispatch`; `red-pdf-sha.txt`: unsupported SHA argument / missing artifact verifier. | `email_command`, `dispatch`, `verify_delivery_artifact`, delivery CLI/pipeline SHA check. Same tests pass; real ledger/snapshot and arbitrary-CWD dry-run tests supplement them. |
| F3 | `test_f3_remote_status_documented_honestly`; `red-wrappers.txt`: missing honest Render-source statement. | Status documentation corrected; `render.yaml` remains unchanged, with no invented transport or status env. Test passes. |
| F4 | `test_f4_cron_mapping_is_explicit`; `red-wrappers.txt`: `KeyError: 'weekly_schedule'`. | Manifest mapping tested through `ZoneInfo`; templates and continuation use 16/17/18/18:30 CST, snapshots retain UTC. Test passes. |
| F5 | `test_f5_f8_f6_recovery_and_continuation_remain_gated`; `red-runbook.txt`: replacement report/drafts absent. | Safe revert-PR and supported cron-CLI draft above. Test passes; manager copy/approval remains separate. |
| F6 | No fresh reviewer is claimed. Documentation test is only a guard against dropping the requirement. | Manager must obtain a real fresh read-only reviewer on the final tree; pending. |
| F7 | `test_f7_preflight_missing_inputs_never_writes` for all four wrappers; `red-wrappers.txt`: no `preflight_failed` output and no read-only mode. | Explicit preflight validates paths/config before dispatch. All four subprocess tests pass. No production provisioning was invented. |
| F8 | Combined recovery/continuation guard in `red-runbook.txt` failed before the draft existed. | Honest prerequisite-first observation prompt above; old paused job not resumed. Test passes; manager copies the prompt. |
| F9 | `test_f9_registry_runner_has_true_dry_run`; `red-email-registry.txt`: `unrecognized arguments: --dry-run`. `test_registry_isolated_dry_run_records_exit_without_claiming_sync`; `red-registry-result.txt`: no scheduler snapshot. | Runner's real dry-run/expected SHA; wrapper gate and actual command; isolated eligible snapshot records `registry_dry_run_exit_N` with `not_dispatched`. Same tests pass. |
| F10 | `test_full_chain_all_four_consumers`; `red-full-chain.txt`: full-chain module absent. During integration, delivery rejected missing Pillar A and publisher rejected `same_run_canonical_title`. `test_full_chain_failure_still_records_before_after`; `red-failure-snapshots.txt`: only one snapshot call. | Real A/B merge, evidence adapter, v2 driver, sidecar, PDF/delivery dry-run, publisher no-push validator and Registry dry-run now pass; all snapshot domains checked and failure snapshots retained. `final-focused.txt`. |
| F11 | Initial monitor fail-closed tests failed on baseline; arbitrary-CWD positive test added later. Audit/code inspection established the original relative runner path. No separate original-CWD red is claimed. | Absolute wrapper/helper/runner paths. `test_f11_explicit_dry_monitor_runs_from_unrelated_cwd_without_seen_commit` passes and verifies no seen or scheduler commit. |
| Email sibling | Real monitor ledger/status SHA and PDF tamper tests, public CLI argument checks. | Same-date completed monitor and expected SHA; exactly four recipients loaded from config; raw child output suppressed. |
| Publisher sibling | Four-wrapper preflight red included publisher; full-chain report validator caught the same-title URL incompatibility. | Existing paths required; absolute publisher with explicit report date and lock; dry-run validates without push. |
| Registry sibling | `red-registry-result.txt`: missing result snapshot. | Actual runner result code projected only into isolated eligible Registry pending slot, never completed. |
| Dry-run provider sibling | `test_weekly_cli_honors_explicit_offline_provider_before_driver`; `red-offline-provider.txt`: `KeyError: 'providers'`. | Existing loopback seam now forwarded through weekly driver; no dry-run live provider fallback. Test passes. |
| Weekly render sibling | Full chain rejected `weekly report is missing the Pillar A section`, then `same_run_canonical_title`. `red-lane-order.txt` exposed document-before-website sidecar mismatch. | v2 serialization and verified-bundle URL policy; existing sidecar lane order and unresolved count mapping tested. All consumers pass. |
| Scheduler test sibling | `test_update_slot_atomic_replacement`; `red-thread-clock.txt`: `PytestUnhandledThreadExceptionWarning`, execution timestamp out of bounds. | Test fixture clock pinned; assertions preserved. `green-thread-clock.txt` passed with thread warnings promoted to errors. |

| Inspected sibling call site | Disposition / AC |
|---|---|
| `scripts/weekly_wiki_refresh.sh` | Retained compatibility/public publisher wrapper; already uses an absolute Python script. New Hermes publisher validates before calling the public Python publisher directly. AC-10. |
| `scripts/step9_update_website.py` | Compatibility caller of the old publisher wrapper; no production-chain call added. Supported test env resolves its historical path; no unrelated rewrite. AC-6/10. |
| `scripts/step8_sync_registry.py` | Compatibility exact-date Registry entry; new Hermes wrapper reaches `weekly_registry_refresh.py`, so no duplicate Registry implementation. AC-10. |
| `scripts/record_weekly_run.py` | Existing append-only ledger CLI, not a delivery CLI. Incorrect pipeline-doc references fixed; public ledger utility retained. AC-1/6. |
| `scripts/scheduler_status.py` | Existing standalone CLI retained for compatibility; new helper reuses `update_slot` directly. No new state service. AC-3. |
| `climate_monitor/job_status.py`, `api_server.py`, `render.yaml`, Compose status override | Reader/503 contract and optional local mount capability preserved. No remote mount or transport invented. AC-3. |
| Legacy report renderer, v1 authoring, numbered step scripts | Default rendering/selection remains backward compatible. Only validated v2 output opts into weekly serialization; existing compatibility tests pass. AC-2/5/6. |
| `climate_registry/selection.py` callers besides verified publisher bundles | Duplicate-title rejection remains the default; legacy selection tests pass unchanged. AC-5. |
| `/root/.ops/.../continuation-prompt.md`, backup rollback runbook | Read for audit only. Draft replacements above; manager owns copying. AC-8/12. |
| Host security, production cron, Render env, production DB | Excluded from writes; no commands dispatched to modify them. AC-10/12 and repository scope. |

The raw scoped search is `sibling-search.txt`. No historical sources, state,
compatibility entrypoints or host-security configuration were deleted.

## Verification and staging limitations

- `python3 -m pytest tests/ -q`: collection failed because the existing Step 9
  module accesses an inaccessible `/home/ubuntu` path (`pytest-full.txt`).
- With supported `CLIMATE_WIKI_HOME` set to this worktree, collection succeeded,
  but the run hung on the existing TestClient/AnyIO portal at the fourth test.
  Interrupted with exit 130; exact output was `...` (`pytest-full-configured.txt`).
  A single-test diagnostic timed out with its full stack in
  `api-hang-diagnostic.txt`. No API test was weakened to hide this.
- Broad diagnostic excluding modules containing TestClient: 1329 passed,
  16 failed, 1 skipped, 1 warning in 135.33s (`non-api-regression.txt`). Five
  capture tests and one Unix-socket Compose test fail with sandbox socket
  `Operation not permitted`; ten legacy Step 3 subprocess tests access an
  inaccessible `/home/ubuntu/web_listening` state path.
- Re-running all 50 pipeline-script tests with both supported test path variables
  (`CLIMATE_WIKI_HOME` and `CLIMATE_WL_STATE`) passed (`legacy-configured.txt`).
- Final affected-code regression: 366 passed in 34.20s (`final-focused.txt`),
  including canonical 57/42/15, tamper, full chain, wrappers, delivery,
  publication, selection and Registry-runner tests.
- Shellcheck is unavailable. The fallback checks each wrapper with `bash -n`
  under `set -e`; it passes. Note that a single `bash -n` invocation with many
  filenames parses only the first script, so the per-file loop was also run.
- Compose config resolves successfully. Build first failed because Docker's
  default config directory is read-only; using an isolated `/tmp` Docker config
  reached the daemon check, which is denied by this sandbox. No build pass is
  claimed; the manager/CI must complete it.
- Staging failed: the worktree index lock is under the read-only Git metadata
  path. `staging.txt` records the exact refusal. The changes remain unstaged;
  there is no staged diff and no new branch commit. The manager must stage them.

Current branch HEAD remains `8a195995b7699e84e297a08a742f9988348d4257`.
Initial production-checkout observation and final readback both returned:

```text
8a195995b7699e84e297a08a742f9988348d4257
## main...origin/main
```

The final readback is `production-after.txt`. Only read-only HEAD/status queries
were used on that checkout. Synthetic temporary Git repositories created by the
acceptance tests are separate fixtures; they are not branch lifecycle operations
on either application checkout.

The full-chain scenario and snapshot machinery passed against an isolated
stand-in checkout/state/remote ref. No real-production full-chain snapshot PASS
is claimed: actual runtime path provisioning and real upstream acquisition remain
unverified. Normal-run observation, fresh review, full CI and Docker smoke remain
required before closeout.

Additional sibling readback: `scripts/step1_pillar_a.py` still reads its existing
SQLite changes/state paths and is not proof of a new #67 acquisition. It is
retained as compatibility and is not invoked by the blocked production monitor.
`step3_aggregate.py` and `step5_build_md.py` remain legacy public consumers;
there is no repo-owned `article_tracker` or standalone `weekly_driver` production
entry to substitute. `_author_57_record_fixture.py` is a manual test-fixture
authoring utility with no production call sites; it was not run or repurposed.
`sibling-search-final.txt` records the date/driver/cron search.

## Exact gate output appendix

Successful `bash -n scripts/hermes_job_*.sh`, the per-file `set -e` syntax loop,
`python3 -m compileall -q climate_monitor climate_delivery climate_registry scripts`,
`node --check showcase/app.js`, and `git diff --check` produced no stdout/stderr
and exited 0. Their zero-byte output files are retained in the evidence bundle.
The default Compose config output below contains empty API key/reload-token values.

### shellcheck scripts/hermes_job_*.sh — exit 127

```text
/bin/bash: line 1: shellcheck: command not found
```

### python3 -m pytest tests/ -q — exit 2

```text

==================================== ERRORS ====================================
_______________ ERROR collecting tests/test_pipeline_scripts.py ________________
tests/test_pipeline_scripts.py:17: in <module>
    from scripts import step9_update_website
scripts/step9_update_website.py:21: in <module>
    if not PYTHON.exists():
           ^^^^^^^^^^^^^^^
/usr/local/lib/hermes-agent/.hermes-runtime/python/generation-1787104628-2780559-20544af3/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/pathlib.py:1235: in exists
    self.stat()
/usr/local/lib/hermes-agent/.hermes-runtime/python/generation-1787104628-2780559-20544af3/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/pathlib.py:1013: in stat
    return os.stat(self, follow_symlinks=follow_symlinks)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   PermissionError: [Errno 13] Permission denied: '/home/ubuntu/climate_monitor_wiki/.venv/bin/python'
=========================== short test summary info ============================
ERROR tests/test_pipeline_scripts.py - PermissionError: [Errno 13] Permission...
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 5.29s
```

### Configured full pytest — interrupted, exit 130

```text
...
```

### Final affected-code regression — exit 0

```text
........................................................................ [ 19%]
........................................................................ [ 39%]
........................................................................ [ 59%]
........................................................................ [ 78%]
........................................................................ [ 98%]
......                                                                   [100%]
366 passed in 34.20s
```

### Legacy pipeline tests with supported explicit paths — exit 0

```text
..................................................                       [100%]
50 passed in 48.73s
```

### docker compose config — exit 0

```text
name: climate_monitor_wiki-issue-87-followup
services:
  caddy:
    container_name: climate-wiki-caddy
    depends_on:
      wiki:
        condition: service_started
        required: true
    environment:
      SITE_HOST: 172.31.10.77
    image: caddy:2-alpine
    networks:
      default: null
    ports:
      - mode: ingress
        target: 80
        published: "80"
        protocol: tcp
      - mode: ingress
        target: 443
        published: "443"
        protocol: tcp
    restart: unless-stopped
    volumes:
      - type: bind
        source: /opt/worktrees/climate_monitor_wiki-issue-87-followup/Caddyfile
        target: /etc/caddy/Caddyfile
        read_only: true
        bind:
          create_host_path: true
      - type: volume
        source: caddy_data
        target: /data
        volume: {}
      - type: volume
        source: caddy_config
        target: /config
        volume: {}
      - type: volume
        source: caddy_logs
        target: /var/log/caddy
        volume: {}
  wiki:
    build:
      context: /opt/worktrees/climate_monitor_wiki-issue-87-followup
      dockerfile: Dockerfile
    container_name: climate-wiki-app
    environment:
      OPENAI_API_KEY: ""
      OPENAI_MODEL: gpt-5.4-mini
      RELOAD_TOKEN: ""
    expose:
      - "8501"
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import urllib.request;urllib.request.urlopen('http://localhost:8501/api/config').read()
      timeout: 5s
      interval: 30s
      retries: 5
      start_period: 20s
    image: climate-monitor-wiki:local
    networks:
      default: null
    restart: unless-stopped
    volumes:
      - type: bind
        source: /opt/worktrees/climate_monitor_wiki-issue-87-followup/wiki
        target: /app/wiki
        read_only: true
        bind:
          create_host_path: true
      - type: bind
        source: /opt/worktrees/climate_monitor_wiki-issue-87-followup/sources
        target: /app/sources
        read_only: true
        bind:
          create_host_path: true
      - type: bind
        source: /opt/worktrees/climate_monitor_wiki-issue-87-followup/article_metadata
        target: /app/article_metadata
        read_only: true
        bind:
          create_host_path: true
networks:
  default:
    name: climate_monitor_wiki-issue-87-followup_default
volumes:
  caddy_config:
    name: climate_monitor_wiki-issue-87-followup_caddy_config
  caddy_data:
    name: climate_monitor_wiki-issue-87-followup_caddy_data
  caddy_logs:
    name: climate_monitor_wiki-issue-87-followup_caddy_logs
```

### docker compose build --pull — exit 1

```text
failed to update builder last activity time: open /root/.docker/buildx/activity/.tmp-default256317591: read-only file system
```

### DOCKER_CONFIG=/tmp/issue87-docker docker compose build --pull — exit 1

```text
permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock: Head "http://%2Fvar%2Frun%2Fdocker.sock/_ping": dial unix /var/run/docker.sock: connect: operation not permitted
```

### Worktree staging — rejected

```text
fatal: Unable to create '/opt/climate_monitor_wiki/.git/worktrees/climate_monitor_wiki-issue-87-followup/index.lock': Read-only file system
```

### Final production checkout HEAD/status

```text
8a195995b7699e84e297a08a742f9988348d4257
## main...origin/main
```

Full red traces, the API hang stack, broad diagnostic failures, and the proposed
complete patch (including new files) accompany this handoff in
`/tmp/issue87-evidence.tar.gz`. `proposed.patch` is a review artifact, not a staged
index or a commit. The manager can stage this worktree after review in the
permitted Git lifecycle environment. No PR was created or edited here.
