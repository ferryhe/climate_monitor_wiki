# Continuous integration

GitHub Actions runs the `CI` workflow for pull requests, pushes to `main`, and
manual dispatches. It has four stable check names:

- `python-tests (3.11)` and `python-tests (3.12)` remain the required Python
  checks. CI runs two isolated whole-file shards for each Python version. Both
  required checks wait for all four shards and fail if any shard fails, is
  cancelled, or is skipped. Shards install only `requirements.txt`, run
  `pip check`, save a full per-test JUnit timing report even on test failure,
  and print their slowest module totals. A shard is assigned by collected test
  count, with ties resolved by file path; the partition test checks coverage,
  disjointness, whole-file grouping, deterministic assignment, and the greedy
  balance bound.
- `repository-checks` compiles the core Python packages, checks
  `showcase/app.js`, and runs `git diff --check` over the full pull-request
  change range.
- `docker-build-smoke` builds the repository Dockerfile without publishing the
  image, then runs an offline, unmounted container smoke with `--network none`.

The repository has no Node.js version declaration or frontend package manager.
CI therefore uses Node.js 24, the current supported LTS line, only for the
JavaScript syntax check. This does not introduce a frontend build system.

The workflow has read-only repository permissions, does not reference
repository or environment secrets, and disables checkout credential
persistence. It contains no schedule, deployment, image push, repository
write-back, production access, or Hermes integration.

Equivalent local checks are:

```bash
python -m pytest -q
python -m pytest -q --ci-shard=1/2 --junitxml=junit-1.xml
python -m pytest -q --ci-shard=2/2 --junitxml=junit-2.xml
python -m compileall climate_monitor climate_registry
node --check showcase/app.js
git fetch origin main
BASE_SHA="$(git merge-base HEAD origin/main)"
git diff --check "$BASE_SHA...HEAD"
CLIMATE_REPOSITORY_COMMIT_SHA="$(git rev-parse --verify HEAD)"
docker build --pull \
  --build-arg "CLIMATE_REPOSITORY_COMMIT_SHA=$CLIMATE_REPOSITORY_COMMIT_SHA" \
  --tag climate-monitor-wiki:ci .
```

The normal local command still runs every test once. CI runs both shard commands
in separate jobs for each Python version, then always-run aggregates preserve
the two required Python check names. Both aggregates require all four shards to
pass. `fixtures-umask0002`, `repository-checks`, and `docker-build-smoke` remain
separate checks.

The fetch and merge-base steps make the whitespace check cover the complete
pull-request range against the latest `origin/main`, rather than only local
working-tree changes.
