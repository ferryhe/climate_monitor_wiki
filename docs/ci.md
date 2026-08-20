# Continuous integration

GitHub Actions runs the `CI` workflow for pull requests, pushes to `main`, and
manual dispatches. It has four stable check names:

- `python-tests (3.11)` and `python-tests (3.12)` install only
  `requirements.txt`, run `pip check`, and execute the complete pytest suite.
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
python -m compileall climate_monitor climate_registry
node --check showcase/app.js
git fetch origin main
BASE_SHA="$(git merge-base HEAD origin/main)"
git diff --check "$BASE_SHA...HEAD"
docker build --pull --tag climate-monitor-wiki:ci .
```

The fetch and merge-base steps make the whitespace check cover the complete
pull-request range against the latest `origin/main`, rather than only local
working-tree changes.
