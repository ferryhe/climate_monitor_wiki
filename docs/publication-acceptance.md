# Read-only Registry and consumer acceptance (G6)

The existing Registry CLI's `accept-publication` subcommand inspects an existing external Registry.
It never approves, enriches, imports, migrates, refreshes display, renders files,
reloads, synchronizes, changes an active pointer, or calls a running service.
Its only write is the new, explicitly designated JSON evidence report.

```sh
python -m climate_registry accept-publication \
  --database /absolute/external/registry.sqlite3 \
  --selection /private/approved-subset.json \
  --captures /private/surface-captures.json \
  --runtime-dir /private/runtime-wiki \
  --public-export /private/public-registry.json \
  --report /private/new-acceptance-report.json
```

All options after `--database` except `--report` are optional. The destination
must not already exist, overwrite any input, or reside in runtime projection
storage. Exit 0 means the selected target and all four captured surfaces match;
exit 1 means blocking, including missing or fixture-only evidence. Invalid CLI
arguments exit 2. Missing/invalid business facts yield an unverified Registry
result; they are never repaired by a read.

The expected target comes from a single canonical Registry read transaction:
public revision, visibility, exact published candidate, candidate hash/identity,
approval receipts and the existing permitted public projection. It bypasses a
configured static Git export. The display cache is observed separately; it
cannot supply expected facts. The module reuses G1 date/content packages, G3's
review projection and separate comparison/inheritance baselines, approved
identity relations, native public DTOs and the existing pure page renderer.
Rendering an expected page happens in memory; no Wiki files are generated.

The result separates `registry_publication`, `local_display`, optional
`local_artifacts`, and `surfaces.display_api/wiki_export/rag/chat`. Registry
publication PASS means that the requested approved subset really is published,
including visibility and policy eligibility as recorded. A held entity can
retain an older approved version. When approval's display refresh rolls back,
the requested candidate is not published and the Registry result fails even
if some old surface is still readable.

## Exact subset

The selection is a JSON array. Approved entries require the exact approved
candidate SHA. Held entries require the previous approved SHA, or JSON null
for a new entity which has never been published. Every held identity must resolve
to a real Registry entity, publication, hash-validated candidate or approved alias;
a null baseline cannot turn a misspelled or unknown identity into an accepted hold.
Omitting selection inventories
all Registry publication identities and returns Registry status `unverified`:
no exact approved/held acceptance scope was selected. Each row records observed
published/unpublished state and pending-candidate coverage; the report separately
lists unselected identities. Inventory counts do not assert a batch was approved.
Entity kinds are `article`, `pdf_article`, `meeting`, `pdf_meeting`.
PDF Dates use Registry `event_id`; occurrence IDs remain separate provenance.

For a PDF absorbed into an approved Core article, the public PDF DTO and exact
document/page/occurrence ownership supply its current serving relationship.
`candidate_basis` defaults to `publication`, naming the original PDF publication
SHA if one exists. Use `candidate_basis: "canonical_serving"` explicitly when the
selection SHA names its approved Core serving candidate. The report separately
retains requested/public/serving identities, the selected publication candidate,
original PDF candidate/receipts (including their genuine absence), Core serving
candidate/receipts and occurrence ownership. It never invents a PDF approval or
requires a duplicate standalone PDF Wiki page after canonical absorption.

```json
[
  {"entity_kind":"article","entity_id":"approved-id","disposition":"approved","candidate_sha256":"<exact SHA>"},
  {"entity_kind":"pdf_article","entity_id":"held-existing-id","disposition":"held","baseline_candidate_sha256":"<previous SHA>"},
  {"entity_kind":"meeting","entity_id":"held-new-id","disposition":"held","baseline_candidate_sha256":null}
]
```

Native website rotation and search share the same article/event contract; PDF
provenance and canonical relations remain attached to it. A date-only/manual
publication may inherit unchanged body, summary and note. Its source verification
is reported as inherited, not a new source-fact check. The evidence report retains
the two baselines separately and hashes the actual changed tables/display fields.
Month precision keeps the literal evidence and 15th midpoint convention. Event
intervals/deadlines are not article publication/update dates. G1 computes the sort
date; an operation or reload timestamp never supplies it.

## Captured surfaces

The capture index is an object keyed by `display_api`, `wiki_export`, `rag`,
`chat`. Each value references an existing JSON file with observed bytes:

```json
{
  "rag": {
    "path":"/private/rag-observation.json",
    "sha256":"<SHA-256 of exact captured file bytes>",
    "observed_at":"2026-10-27T12:00:00Z",
    "endpoint":"<actual inspected endpoint or local surface path>",
    "command":"<actual observation command>",
    "evidence_kind":"captured"
  }
}
```

The observer is responsible for authentic collection and retaining the original
response/runtime context. A hash establishes byte identity, not trustworthy
provenance. Do not label test fixtures or a hand-constructed target projection
`captured`. Missing/other `evidence_kind` values produce `fixture_match` at most
and block overall completion. A capture only proves what was observed at its
recorded time; this tool does not independently contact or authenticate a remote
service. Do not reuse an old observation to assert present activation.

An observed file has these surface-specific facts:

* `activation`: actual `state`, `generation_id`, `registry_revision`, and
  `mappings` containing Registry entity kind/id, `candidate_sha256` and
  `facts_sha256` for API/export DTOs. `state` must be `active`. Mapping IDs may
  differ between layers. A header/status/generation name alone is insufficient:
  the actual content below must match independently.
  A recorded prepared/inactive state or an existing wrong candidate/content
  mapping fails acceptance. Absent state/mapping evidence remains unverified;
  an inspected in-memory corpus marked loaded does not assert service activation.
* API/export `records`: current native public payloads wrapped as
  `{entity_kind, entity_id, payload}`. Article and Dates source labels,
  publisher, summary/content package, date precision/basis, current versions, PDF
  document/page/occurrence provenance and relations are compared.
  Article/Dates relations retain their complete approved body binding, event
  identity and event facts; a provenance-only relation cannot satisfy this contract.
  Multiple PDF
  calendar occurrences of one event are grouped by event ID, retaining all
  occurrences. If recording historical rows, mark them `role: "history"`;
  they cannot satisfy a current row or become a duplicate current body.
  `record_totals` gives each kind's actual unfiltered pagination/export total;
  all current rows must be captured to prove an absent held entity. A partial
  page, filtered query or missing lookup is not absence evidence.
* API/export `article_order`: observed `[entity_kind, entity_id]` pairs in
  current list order. Two or more selected articles must follow G1's order.
* Wiki/export `pages`: actual filename-to-Markdown values, with `manifest`
  containing the observed `registry_revision`, filename-to-byte-SHA `pages`,
  and `pdf_occurrence_ids`. File existence alone does not satisfy this check.
* RAG/Chat `documents`: actual corpus document ID to `{page, markdown}`. Full
  current page content must match the independently rendered approved page.
  `queries` contain actual query text and `hits` with entity kind/id,
  `candidate_sha256`, `document_id`, `excerpt`, and optional `role` (`history`
  cannot prove current retrieval). A current hit must be from the correct
  current document and its excerpt must occur in the observed approved text.
  Hits preserve actual `date`, `date_kind` and `date_metadata` from the loaded
  chunk/source record; native flattened source metadata is also accepted. G1's
  content date, basis, literal evidence, precision, authority, normalization and
  applicable version/candidate fields are compared separately from page text.
  Stale observed metadata fails; missing actual fields stay unverified. The
  collector never fills actual fields from the frozen target or page Markdown.
  Each targeted query preserves its requested `target` kind/id separately from
  hit identities. A query explicitly recorded as `status: "unqueryable"` (or
  unavailable/failed), or a query that was never observed, stays unverified.
  `corpus_document_count` is the actual loaded corpus count and must equal the
  complete captured document inventory, including retained historical documents.
* Chat queries also contain the actual `answer` and `citations`, each identifying
  `document_id`, `candidate_sha256`, `excerpt`, and the actual cited source's G1
  date metadata. Citation identity/excerpt must match the current retrieved source;
  the synthesized answer need not copy that excerpt verbatim. An accessible answer with missing/wrong
  citations, a generic answer, an empty/history-only targeted retrieval, or a stale
  current document/version fails content acceptance. An absent or unqueryable
  capture remains unverified.

An approved substantive content package freezes two query requirements: current
approved summary/key content and the supported change note. Valid current hits
must actually retrieve both, and Chat must demonstrate both in its cited answer;
coverage may accumulate across targeted queries/hits. An unchanged heading or
old unchanged passage is insufficient even with correct candidate/generation IDs.
The report records retrieved/answered coverage separately and hashes the frozen
requirements. Inherited date-only publication keeps the same content requirements
without claiming a new source-content verification.

Answer coverage accepts conservative factual-clause paraphrases with normalized
inflection/punctuation and equivalent increase/decrease wording. Numeric values,
order, negation and uncertainty remain significant. A revised-estimate comparison
such as “$20 billion, double the earlier $10 billion” can cover one approved
numeric change only when the approved before/current excerpts, units and arithmetic
support it. The bounded form “Flood losses are now estimated at 20 billion, up from
10 billion” also preserves the approved topic and numeric direction. Both forms
must retain the approved summary/note's remaining material words, including
uncertainty, negation and other qualifications. This is source-bound coverage, not a general semantic truth judge:
generic text, unsupported wording, negated claims and reversed values cannot
borrow a correct citation to pass. Exact source excerpts and candidate bindings
remain independently checked regardless of answer wording.

`observe_rag(existing_knowledge_base, requests)` reads an already loaded
`WikiKnowledgeBase` and executes its real `search` method. Each request has
`entity_kind`, `entity_id`, `query`. It copies observed documents/hits, never
expected text or regenerated pages. Requested targets remain separate from actual
hit identities. It preserves indexed entity metadata, or derives an article ID
from its indexed article page path. A PDF occurrence/shared Dates page without
parent-entity metadata retains unknown entity identity instead of borrowing the
requested target. The checker can resolve that identity only when the observed
page and candidate SHA uniquely bind one approved target. Missing/ambiguous
identity stays unverified; an observed wrong page/version remains a failure.
It deliberately reports loaded activation
as unverified until independent evidence identifies the active generation.
It does not construct/reload a responder or generate a Chat answer.

## Existing local protocols and evidence limits

The stored display reader is used directly on the canonical read-only
connection; `display_snapshot` is excluded because it opens `mode=rw` for WAL
management. Its match proves local stored DTOs, not the remote API.

`--runtime-dir` uses the existing active pointer, SHA-checked intake manifest,
SHA-checked approved Registry snapshot and its public revision. It compares the
actual Markdown bytes and PDF allowlist. A prepared generation or reload receipt
does not prove a serving Wiki, RAG corpus or Chat instance. `--public-export`
checks the existing export artifact hash/policy, DTOs and Markdown. Export v1
has no full canonical visibility/revision/activation binding, so a content match
still reports unverified activation. Neither optional check substitutes for a
captured surface.

The final Registry read detects revision changes after freezing. A frozen match
is never called current latest after that change. If the final read is unavailable,
the report retains the frozen target and marks latest state unknown. Likewise an
unqueryable capture may retain `last_observed_generation_id` only; a timeout does
not prove a remote instance stayed old.

The sanitized report keeps exact target/revision/candidate identities, receipt
and source capture/body/PDF references, per-field/content/page/query/citation
hashes, source paths, generation mappings, observation time, sanitized endpoint,
command hash, gaps and next owner. It omits raw source bodies/summaries, receipt
text, URL credentials/query strings and private absolute input paths. Keep the
original captured bytes privately by SHA for inspection. Fixture tests, HTTP 200,
health status and historical deployment evidence are not full online proof.
Actual three-source production acceptance remains an explicitly authorized
operator task; this entrypoint performs no activation or repair.
