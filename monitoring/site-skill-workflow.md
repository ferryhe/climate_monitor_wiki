# Per-source collection skill workflow

The 36 `climate-site-*` skills under `.agents/skills/` each contain operator
notes and a short `Hermes production guidance` section for one `source_key`.
New acquisition bindings copy that section and its hashes into the immutable
run binding; Hermes receives only the selected sources' short hints. These
hints guide search, navigation and interpretation. They are not evidence and
do not change Hermes jobs, `web_listening` tool selection, reader policy, or the
frozen source scope. They are not generated `web_listening` SiteSkill values.
Read the selected source's entries in `monitoring/supranational_sources.yaml`
and `monitoring/site_scopes.yaml`; those files remain authoritative for source
identity, seeds and allowed paths.

## Work on one source

1. Freeze the selected `source_key`, its reviewed scope, the upstream runtime
   revision and a separate diagnostic run identity. Do not edit the active
   scheduled task to isolate a source. Use an isolated run root; if the run
   reaches Registry, use a separate candidate there too.
2. Let `web_listening` explore or refresh each configured seed through its
   public governed API. Inspect the returned attempts, actual tool IDs, policy
   decisions, missing pages and partial coverage. `fetch_mode` in the current
   YAML is a historical hint: the current adapter records it as
   `requested_engine`, but does not pass it as a tool-selection command.
   Browser installation/qualification belongs to the upstream lifecycle in the
   same Runtime data directory used by the run. Establish actual browser use
   from the attempt receipts.
3. Verify the exact fetched body and its Markdown derivative before treating a
   URL as an article. Preserve the source URL, raw and Markdown hashes, MIME,
   publication-date evidence, content references and all failed attempts. For
   meetings, extract event dates and status from the bound article body; do not
   substitute the article's publication or observation time for an event date.
4. Pass verified observations through the existing acquisition batch and
   Registry contracts. A single-source diagnostic result is not a complete
   weekly report, PDF or deployed Registry sync. Record any blocked, failed or
   unresolved coverage as such.

For a bounded seed diagnostic, run:

```bash
python scripts/inspect_source_site.py --source-key KEY \
  --state-dir /external/state/KEY --runtime-dir /external/runtime
```

Optionally pass `--seed-url` with one exact configured seed. The script writes
an evidence summary outside the checkout and exits 2 for partial, incomplete
or blocked seed coverage. A source skill may add its own `scripts/` only when
verified site behavior needs distinct deterministic work;
such a script must call the governed reader instead of implementing another
network fetcher. There is no benefit in 36 identical wrappers.
Use a fresh external state directory when comparing first-pass coverage across
sites. Reusing a successful checkpoint invokes refresh, which may visit more
pages and exhaust the same bounded request budget.

## Where to keep results

| Put in Git | Keep on persistent runtime storage outside the checkout |
| --- | --- |
| Reviewed seed/path changes in `site_scopes.yaml`; source-specific, dated operating findings in the corresponding `SKILL.md`; shared workflow changes here | Generated `web_listening` SiteSkill and SiteState checkpoints, fetched bodies, run ledgers, Registry databases, credentials and temporary artifacts |

On 2026-09-28, TypeSafe Choice recommended both the repo-instructions/runtime-state
split (`repo_skills_runtime_external`, reported confidence 1.0) and freezing the
selected short Hermes guidance plus hashes in each new run binding
(`source_keyed_frozen_prompt_guidance`, reported confidence 1.0). Reviewed scope
and operating guidance belong in version control; generated checkpoints change
on each run. This does not verify any site's access.
For scripts, it recommended one shared runner and optional site-specific scripts
(`shared_runner_optional_site_scripts`, reported confidence 1.0); actual site
evidence remains the requirement for adding one.

`web_listening` creates a data-only SiteSkill only after successful exploration.
Validate its identity, digest, tool and discovery evidence before saving it with
the corresponding SiteState. Do not synthesize a SiteSkill from a YAML hint or
manually edit a live checkpoint. Back up runtime state before migration or reset.
The current managed state default is inside the checkout unless
`CLIMATE_MANAGED_STATE_DIR` selects an external persistent directory; check the
effective bound path before a live run.

When a site changes, update only that source's skill and authoritative scope,
then repeat its isolated run. Changes to the governed reader or tool selection
belong in `web_listening`, not a parallel climate crawler. A URL or report
imported from elsewhere needs its own verified source evidence and the same
Registry contract; it must not impersonate a successful site fetch.
