# Item review handoffs

G8 uses the existing external review root and Registry. It adds no Registry
tables, scheduler, reviewer dispatch or notification delivery. Immutable Registry
candidates, exact reviews and published pointers remain authoritative. The
external `item-lifecycle/state.json` retains pending candidate references,
disputes, actual replies and date execution manifests/results. A date-only
publication therefore does not lose a pending content candidate.

Use the existing entrypoint, with a real external root and Registry:

```sh
python scripts/review_pipeline.py status --kind acquisition --root RUNS --database DB
python scripts/review_pipeline.py export --kind acquisition --root RUNS --database DB --item CANDIDATE_SHA
python scripts/review_pipeline.py hold --kind acquisition --root RUNS --database DB --result dispute.json
python scripts/review_pipeline.py import-decisions --kind acquisition --root RUNS --database DB --result replies.json
python scripts/review_pipeline.py resume --kind acquisition --root RUNS --database DB
python scripts/review_pipeline.py prepare-owner --kind acquisition --root RUNS --database DB --result checks.json
```

`status` and `export` are read-only. Export is a JSON handoff, not evidence that
anyone received a notification. `hold` takes `candidate_sha256`, the original
`text`, `evidence`, `options` and `impact`. An unchanged dispute returns the same
ID with `created: false`. No reply and elapsed time confer no authority.

`import-decisions` takes a nonempty JSON list. Each reply has `decision_id`,
`kind`, actual human `actor`, `reference`, `received_at`, original `text` and
`target: {entity_kind, entity_id}`. A `business` reply also names the exact
`dispute_id`, `candidate_sha256` and `choice`. Import records the reply only;
it supplies no source fact or review PASS. Changed dispute inputs require
reconciliation. The same decision ID and payload is a no-op; a conflicting
payload using that ID is rejected.
The current candidate's exact dispute set is checked before historical disputes.
All current disputes need actual replies; old replies never authorize a new SHA.
Historical disputes and their replies remain in the handoff.

A `human_date` reply instead contains the existing G1 manual-date `manifest`.
The reply's text, human actor, reference and target must match its actual manual
authorization. It does not need a reason or fabricated source capture. Execute
only that imported instruction through G1:

```sh
python scripts/review_pipeline.py resume --kind acquisition --root RUNS --database DB \
  --decision-id ACTUAL_REPLY_ID --queue-dir QUEUE --backup-dir BACKUPS
```

Resume first recovers any exact committed receipt, including after loss of its
external result write. It checks current instruction applicability separately
from factual/candidate receipt freshness. Candidate regeneration can produce a
new G1 execution plan while preserving the original reply and authorization;
the exact candidate referenced by that plan is retained before the date writer
can replace its latest pointer, including an unregistered pending content version.
A later conflicting human authority or ambiguous identity stays held. The
existing G1 writer validates dates and record/content-version scope, inherits
the full approved baseline and commits its normal approval/display transaction.
It neither publishes pending summary/update-note fields nor starts a content
author, PDF job or source-fact reviewer. Successful date execution is not replayed
to repair downstream failures.

Preparation isolates complete candidates before native packets or owner lists
are frozen. Actual version-bearing Article/ Dates dependencies propagate a hold;
sharing a report, PDF or run does not. Failed candidate staging rolls back that
whole candidate's savepoint. A native owner keeps its immutable packet until it
finishes. Owner list ingestion/approval still validates and transacts the entire
selected list atomically; concurrent changes require fresh preparation.
Dependency checks also resolve a selected Dates item's Article from the Registry
when the Article is absent from the submitted subset.
Each source relationship binds its exact Article content version, including
pending bodies retained when G1 publishes a date overlay of an older body.
Only the current published body or its verified frozen approval ancestry proves
an approved version; a standalone PASS or display rollback receipt does not.
A business reply restores the disputed Article's independent review eligibility;
Dates still waits for that body's approval. Missing bodies stay held, and a
historical approval cannot bypass an unresolved dispute for that required body.
When a required body is substantively different from the published Article body,
Dates waits for its committed approval even if no business dispute was recorded.
Readiness for independent Article review does not approve that relationship.
An already published identical body and unrelated Article T1 keep their existing
exceptions. An unresolved dispute on the exact current published SHA still holds
its Dates dependencies for the owner; the old Article remains public. Its actual
reply restores those dependencies without redoing the committed approval.
Owner preparation returns normalized `lifecycle_root` and `registry_database`
beside the unchanged `ready` manifests. Pass that root as `--lifecycle-root` to
the existing `approve-owner-check` command (or the API's `lifecycle_root` keyword).
The approval transaction requires the actual matching ledger, verifies the exact
selected candidate/input scope and rechecks the complete list and dependencies.
Missing, unreadable or invalid state fails before backup/write. Dispute saving
uses the same Registry advisory lock before its lifecycle lock, so it cannot
land between that recheck and commit. The root never changes a manifest or
review hash. Explicit G1 human-date instructions retain their separate writer.
`peek`/`pending` only read the current ready set. If the stored packet is empty
or changed, they return `preparation_required` and exact ready candidate SHAs;
they do not stage or write a packet. Actual `claim --target registry-review`
prepares under the existing short review lock, claims that exact packet and
returns it. An active owner continues to own its unchanged original packet.

Status reads current Registry pointers and exact review references. For
an unapproved current candidate, the latest actual `needs_correction` or
`rejected` receipt maps to `needs_correction`, held for its author; the original
verdict and receipt remain visible. A new candidate SHA needs its own review.
Acquisition failures are read from the original immutable receipt and its exact
native claim/archived packet, separately from Registry reviews. They survive
ready-set refresh and a lost mutable-state write. Receipt publication shares
the existing Registry writer lock; a previously claimed final packet must
recheck a later failure before approval. These receipts never establish PASS.
The existing `correct` command also accepts the exact item retained in
`excluded_candidates`. It stages a new private candidate, retains the immutable
old failure receipt, and leaves ordinary T1/preparation/claim readiness and a new
independent review to determine its next step. Refresh retains the correction's
identity and revision while it waits; correcting one item does not clear sibling
approvals or publish its derived fields.
Correction context, exact item/body and authored fields are retained in the
existing correction knowledge evidence within the Registry writer transaction.
Both native claim routes and their final in-lock review gates reject that same
context for the corrected content. `correct` passes its current `--session-id`,
`--execution-id` and `--hermes-database` to the existing native cron identity
check before writing. If `HERMES_SESSION_ID` is present, it must match. The old
review claim remains a historical reviewer fact. Old corrected SHAs with only
that reviewer or an unverified parent context stay blocked for reconciliation
with a distinct correction; they are never rewritten or treated as authored by
the old reviewer. An unchanged correction cannot replace immutable author evidence.
Another native context still needs the complete original scope and actual reads.
With `--queue-dir` and `--runtime-wiki-dir`, it also validates the writer's prepared
generation and actual active pointer against the same public revision. Prepared
is separate from active; missing G6 consumer verification remains `unknown`.
Source attempts retain their real access/verification statuses. G8 does not
invent retry times or replace T1's existing backoff/recovery.

An ordinary resume returns the exact ready/held/invalid handoff with
`execution: not_dispatched`. G4/G7 authoring, G9/G10 role dispatch/review,
G5 activation, G6 consumer verification and G11 cross-stage recovery remain with
their owning modules. A retained B that no longer matches the latest candidate
is explicitly invalid pending author reconciliation: G3 can rebase it over A′
with fresh content review, and rejects it after substantive C supersedes A.
Pending/held fields never change the old approved public pointer.
