# Semantic identity and Article/Dates corrections

`plan-owner-check --identity-correction` extends the existing owner candidate,
independent review and publication path. It does not update source rows, change a
body, generate a summary, or synchronize consumers.

Plan the canonical target and name each existing alias or Dates participant:

```text
python -m climate_registry plan-owner-check --database <external-db> --kind article --id <canonical-id> --identity-correction --identity-participant pdf_article:<source-id> --identity-participant meeting:<event-id>
```

Each factual participant freezes its approved candidate, exact approval receipts, current
publication/visibility pointers and live source snapshot. The manifest supplies
an actual author, authorization/audit references, semantic disposition, literal
retained source evidence and the selected correction. `ingest-owner-check`,
`plan-owner-review` and `approve-owner-check` remain the writers. Record the exact
candidate with the existing item lifecycle before independent approval. A date
instruction uses the separate date scope and cannot approve identity or links.

- `aliases` bind an exact participant source scope to the canonical target.
  Whole-entity mappings retain old IDs as references. An occurrence selection uses
  `selection=occurrences`, an explicit subset of frozen `occurrence_ids`, and null
  body fields; it moves only those observations in the disposable public view.
  It does not merge the source canonical article or transfer its human dates.
- `relations` bind the canonical Article's exact retained body ID and SHA to the
  exact approved event edition candidate. An Article with analysis remains visible
  as an Article and exposes `article_event_relations`; it does not create another
  Article or another Dates identity. An edition, interval, deadline and status
  remain distinct facts from the retained Dates snapshot.
- `canonical_url` changes the approved locator only. Original requested/final/raw
  URLs, captures, bodies, observations and approval receipts remain unchanged.
- `disposition` is explicit: `article`, `article_linked_to_dates`, `pure_event`,
  `portal`, or `irrelevant`. The latter three suppress a current Article; they do
  not invent event facts or article dates.

An exact latest candidate with no published baseline can use a disposition-only
`pure_event`, `portal` or `irrelevant` scope. Its new independent PASS approves
that disposition, while factual eligibility stays false and no Article body is
projected. This case prohibits URL, alias, relation or Article fact changes and
does not absorb other candidates. Independent factual items keep progressing.

A mapping is private until its new exact independent PASS. Changed sources, body,
approval or visibility pointers make the proposed scope stale. Conflicting
approved bodies or human date instructions require the affected correction to be
held; other independently scoped work can continue. Existing content comparison
and approval inheritance baselines remain separate. Approved mappings survive a
later content candidate as immutable history; relations from a different body
remain historical and require new semantic review before becoming current.

Schema 27 removes locator uniqueness from core and PDF article parent tables.
It preserves IDs, rowids, foreign keys, indices, triggers and all child history;
failed migration work rolls back. An acquisition `semantic_identity` is an
explicit material key with a literal retained body excerpt and reason, retained
in source origins. It is never derived from a content hash or a capture time.
URL-only resolution of future captures works only for a unique existing Article
owner. URL equality and an Article classification label do not authorize a PDF/core
merge, even when each URL has only one row. PDF occurrences remain independent until
an explicit retained same-object core link or a new approved exact identity scope
binds them; schema migration and source intake do not manufacture that authority.

Approved DTOs/export retain `identity_mapping` with stable canonical/source IDs,
body/version scopes, occurrence IDs, approval snapshot SHA, review IDs and prior
mappings. PDF modules retain their original `source_pdf_article_id` and expose
real `canonical_article_id`/`module_identity` values. These are the handoff for
G6/G11/G12b; this change does not implement those consumers or production rollout.

Active aliases and exact occurrence ownership are cumulative across later corrections.
Their original scope and approval history stay attached; a later locator correction
cannot revoke an earlier merge. Approved old and current locators resolve future
captures to that same Article unless an explicit material key or ambiguous ownership
requires a distinct selection. Visibility applies to the canonical group, including
old references, while hidden approved mappings still prevent alias republication.

PDF detail IDs retain their own exact source occurrences. Report modules group all
source parents for one canonical Article, expose `source_pdf_article_ids`, and retain
each occurrence once across direct, materialized, paginated and static export reads.

A retained explicit PDF-parent core link follows an approved whole-Article alias group
when later candidates collect current source occurrences, date rows and dependencies.
The original core link and IDs remain unchanged; partial occurrence mappings do not
extend this group to the whole parent.

An approved whole PDF-parent alias also routes later imports to the current canonical
candidate and published comparison baseline. Each new document/occurrence still needs
its current source check and a new applicable candidate approval. The original mapping
receipt does not approve those new facts. Partial occurrence aliases never authorize
future observations of the whole source parent.

Mistaken ownership can be changed only by an explicit `alias_operations` entry:
`{"action":"revoke","alias_sha256":"<digest of exact current approved alias>","evidence_ids":["<new proof>"]}`.
Use `replace` with a new exact `aliases` selection for the same source to narrow or
replace that scope in one review. Freeze the source as a participant, as well as every
inherited group participant being revoked. Empty or omitted aliases still inherit;
they never mean revoke. Operations require a new exact independent PASS and retain the
old alias, candidate and receipt in `revoked_aliases`/`prior_mappings`. Changing the
canonical direction uses an approved revoke, then a separate new merge review; old
history cannot form active ownership cycles. Transferred human dates, pending source
approval or unresolved related-participant scopes hold the correction with an explicit
required scope rather than silently restoring or inheriting facts.

A whole core Article cannot use a PDF parent as its canonical target: that direction
cannot retain the current Article body and old Article entry point. Use the core
Article target and an exact PDF source scope. Partial occurrence mappings still move
only their selected observations.

An explicit material key follows a reviewed locator move only when that key is already
retained in the approved canonical group's own or active whole-Article source origins.
A different key stays a separate material; ambiguous matching roots require review.
Matching URLs or body hashes do not create this authority.

PDF occurrence `source_provenance` is the AC1/6 public export contract for retained
source labels: each source keeps its `filename` and existing `observed_at`, without
local storage paths or arbitrary raw audit objects. Sources are not merged by label.
Range-report nested `source_observations` use the same allowlist in live and static
reads. The enclosing occurrence/document/page and approved candidate references keep
the exact scope; these labels grant no identity or publication authority. Observation
time remains source provenance, not an Article publication date. Complete source
rows, paths and review history remain in the external Registry.

Semantic sidecars for stored reports bind to the exact report SHA/date/filename
and its retained appearance, discovery and version membership. The existing
approved graph may normalize that saved Article ID and authorize a moved locator;
the sidecar URL only selects a unique member within this report. It does not choose
an Article from global URL matches. Apply rechecks the exact membership/version
binding under the writer lock. Original locators/IDs and report rows remain history;
standalone or unpersisted dry previews do not establish database membership.


Native Dates whole-entity aliases retain the original `climate_event_sources` and
`climate_event_versions` IDs and their exact approved content/run dependencies in
the target projection. Current event facts still come only from the target row.
For mapped native Dates, `identity_mapping` carries the approved alias/review scope;
`sources[].event_id` identifies the original event, and `version_history` exposes
only event ID, record version, state SHA, meeting-run ID and recording time.
Historical state JSON, retained source bodies and candidate packets remain internal.
Direct, materialized and static meeting DTOs use the same lineage fields. Later
reviewed updates inherit this history; an explicit reviewed revoke subtracts only
current ownership while preserving raw rows and prior mapping/approval receipts.

PDF report overlay catalogs and module selectors use the existing approved
Article/PDF grouping identity, including exact standalone PDF parent IDs. A URL
is only a locator: independent same-URL editions keep separate modules and page
counts. Repeated public/runtime copies merge only within that identity, retaining
each distinct occurrence ID; this projection grants no new source approval.

A combined PDF parent detail retains an Article-level owner/reference only when
all its current occurrences have that same approved owner. A standalone remainder
keeps its current public PDF parent reference. If every occurrence is absorbed by
different owners and no standalone parent is public, the container's
`record_reference` and `published_candidate_sha256` are null. Existing per-occurrence
mappings resolve to each current public Article detail's entity/candidate/version
reference; the parent ID and source/page evidence remain available.
No hidden parent receipt is promoted into current authority. Direct, stored and
exported details preserve this boundary; partial ownership grants no whole-parent authority.
