# Article categories

`monitoring/taxonomies/article_categories.yaml` is the active category configuration.
It defines the 14 IDs, display labels, descriptions and the 1–4 category limit.
Authoring validation, its evidence prompts and Category Map read this configuration;
there is no separate Python label/hash whitelist or JavaScript category list.
Names must be exact, unique labels. The existing article keywords and Concept Map
remain separate metadata and navigation features.

The general climate focus still applies. Climate Risk is not a mandatory fallback.
Decarbonisation describes measures; Transition Risk describes their risk effects.
Climate Finance & Investment describes financing; Capital & Solvency describes
capital adequacy and balance-sheet risk. Climate & Nature and Climate & Health
require evidence of a climate connection. The configuration contains the detailed
boundaries used during classification.

Changing the YAML changes its byte-derived SHA automatically. New task bindings
and semantic sidecars retain those exact YAML bytes. They continue to validate
against their frozen definitions after the active file changes. Keep
`article_categories_v1.yaml` unchanged: historical v1 artifacts without embedded
definitions still validate against that archive. Portable JSON schemas provide
structure; runtime validation enforces the selected definition's labels, limits,
normalization, uniqueness and exact SHA.

New managed task definitions reference the active file. Existing saved definitions
retain their selected path. To activate it for a saved task, edit its taxonomy path
through the existing management definition editor to
`monitoring/taxonomies/article_categories.yaml`, retaining reference `version: v1`.
This PR does not change a production task, schedule or database. Restart/reload the
application after a configuration change and refresh an open Category Map.

## Reviewed historical corrections

`article_metadata/category-corrections-20261012.json` binds every executable record
to its exact approved candidate, latest pending candidate, visibility, URL and old
categories. Its reasons and evidence references are review evidence. The
`pending_records` section is informational and is never executed.

Rehearse against an external database copy:

```bash
python -m climate_registry correct-categories \
  --database /absolute/external/registry-copy.sqlite3 \
  --manifest article_metadata/category-corrections-20261012.json
```

The default is a read-only dry run. After reviewing the manifest and approving the
target database write, add `--apply --backup-dir /absolute/external/backups`.
Apply uses the existing Registry lock, a SQLite backup and one transaction.
It copies the approved snapshots, changes only category display/provenance (and
the existing PDF manual category mirror), and appends bound candidates, review
receipts and knowledge audit records. Original evidence, titles, summaries,
keywords, dates, visibility and a newer pending candidate remain intact.
A stale record rolls back the whole operation. Repeating the exact manifest
verifies the resulting bindings and does not add duplicate history.

Category Map uses `/api/registry/article-categories`: one compact, approved,
canonical-URL-deduplicated collection plus the configured taxonomy. Registry
articles take precedence over linked PDF observations. Each article contributes
once to a node and once to each category pair. Circle sizes increase with the
distinct supporting-article count; the responsive network leaves room for each
category name and article count, with a taller canvas on narrow screens. All labels remain visible; edge
counts appear on hover, keyboard focus or selection. Supporting-article buttons
open the existing Registry/PDF detail with the retained version and provenance.
