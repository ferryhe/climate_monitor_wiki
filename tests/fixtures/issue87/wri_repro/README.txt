Issue #87 WRI reproduction, taken from the real PR #105 SSH audit.

manifest.json retains all 164 original discovered_items and all run/source
identity fields. Only deprecated/extensions (duplicate raw HTML captures)
are omitted for this public bundle. See PROVENANCE.json for original and
bundled SHA-256. acquisition-batch-result.v2.json is byte-identical.
pillar_b.empty.json is an explicit empty fixture for the URL-merge boundary.
Full originals remain in the SSH audit and the owner's local workspace.

From the existing climate checkout with its dependencies and pinned web package:

python -c "from pathlib import Path; from scripts.run_climate_monitor import _read_prepare_inputs; p=Path('.tmp/issue87-wri-repro'); _read_prepare_inputs(p/'acquisition-batch-result.v2.json',p/'manifest.json',p/'pillar_b.empty.json')"

At e654b2c this exits with:
manifest duplicate canonical url: https://www.wri.org/insights

The original raw export produced the same error in the actual server CLI.
Do not remove or relabel discovery rows to make the test pass. Fix the existing
canonical URL merge path and retain public outcome/source identity validation.
