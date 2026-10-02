# Dormant v2 refusal intake (#158)

This is a test-only, stdlib module. It has no launcher, socket transport,
execution callback, service, browser, or application integration. Never launch
code from a collaborative checkout as root. V1 and its product protocol remain
unchanged. This slice does not implement Issue #158's runner or prove runtime
manifest identity, containment, or a no-business-write network boundary.

## Wire and identity

`encode_message` and `decode_message` handle canonical ASCII JSON objects of at
most 8,192 bytes, with no framing or I/O. Duplicate keys, trailing data,
noncanonical encodings and nonfinite numbers fail closed. `IntakeBroker.handle`
and `verify_reply` also bound and copy their supplied objects through this codec.
The encoded-size check occurs after serializing a Python object: it is an
acceptance limit, **not** a pre-serialization CPU/memory bound. This pure API
may only receive trusted, already bounded local test objects. A future
untrusted transport must receive length-bounded bytes before JSON decoding
and enforce depth/element limits; do not expose this object API as a socket
handler.

Requests have exactly version, audience, key_id, operation, invocation,
invocation_sha256, nonce, expires_at and mac. Version is integer 2. Audience,
purpose and invocation fields use the v1 lexical contract, with the additional
`input_sha256` commitment. No command, environment, input bytes or pathname is
accepted on the wire. Digests commit to bytes; they do not validate the truth of
business bindings, repository identity or runtime manifests.

Dedicated credentials use v1's strict private-file reading and field set, but
require version 2 and a nonzero UID. Credentials are reread for every exchange;
expired, disabled, missing or rotated credentials reject old requests. Requests
expire within 60 seconds and no later than their credential. Nonces are 32
lowercase hex characters. HMAC-SHA256 domains are `host-exec-v2/request\0` and
`host-exec-v2/result\0` (NUL suffix), distinct from v1 and from each other.

The pure broker checks an explicitly supplied peer UID against credential
policy, rejecting root and boolean UIDs. There is **no kernel peer discovery**
and no authenticated network endpoint here. The caller is a trusted local test
harness. Shared-secret holders, same-UID processes and host root remain trusted.

Replies contain exactly version, request_sha256, result and mac. The digest
binds the entire original request including operation and nonce. Result contains
exactly invocation_id, invocation_sha256, status, ready, retryable and cleanup.
Every result has `ready=false`, `retryable=false` and
`cleanup={"status":"unverified"}`. Malformed replies and signed success,
acceptance, readiness or cleanup claims are rejected with the constant v1
`ProtocolError`; no sensitive diagnostic is returned.

## Refusal semantics

| Operation or condition | Result |
| --- | --- |
| New submit with locally sealed input | Persist refusal commitment; `not_ready` |
| New submit without sealed input | Record nonce only; `not_ready` |
| Matching duplicate or readback | Retained `not_ready` or `withdrawn` |
| Same ID with different invocation digest | `conflict`, including after withdrawal |
| Cancel matching refusal | Persist `withdrawn` tombstone |
| Readback or cancel unknown ID | `unknown`; no invocation record |
| Reused key-ID/nonce hash | `replay`; no mutation |
| Full record or nonce capacity | `capacity`; no eviction |

Withdrawal only marks a refusal commitment. It never means a process was
cancelled or cleaned up. No submit returns `accepted`; no record authorizes
execution, creates a queue, or permits later promotion. A future runner must
introduce a **new protocol epoch/version/identity for dispatch** and must never
promote refusal records from this dormant module. There is no upgrade or
migration path in this slice.

## Local sealing, persistence and limitations

Host-local `seal_input` copies 1–262,144 immutable bytes into canonical base64
storage keyed by SHA-256. `input_bytes` is local only. Limits are 64 input blobs,
64 refusal records, 4,096 nonce hashes and 4 MiB total canonical ledger,
whichever fills first. Input/ledger byte limits raise `ProtocolError` without
acknowledging a new record. Records contain only invocation, digest and
refused/withdrawn phase. Inputs and tombstones are never evicted.

Explicit initialization requires an empty, private 0700 directory. It persists
a 0600 marker before the initial ledger. Open never initializes missing state.
Ancestor checks, no-follow descriptor traversal, single-link regular 0600 file
checks and bounded stable reads reuse v1 helpers. Directory flock excludes a
second store; a thread lock serializes mutations and `close()` so it cannot
retire the state descriptor during a write. Writes use exclusive temporary
files, file fsync, atomic replacement and directory fsync before replying.
Persistence uncertainty poisons the instance against further use.

Open requires an `expected_checkpoint` supplied from independent trusted
retention; it must match the SHA-256 of the exact ledger. Missing, corrupt,
unsafe or rolled-back state is rejected. Initialization returns the initial
checkpoint; successful mutations expose the new `store.checkpoint`. Independent
retention is **not implemented**. A test caller must retain each checkpoint
separately before a later reopen. Never derive the expected checkpoint by
reading the ledger being checked. A stale anchor after a write rejects startup;
there is no automatic reset or repair. Marker and revision do not defend against
coordinated rollback or loss of both ledger and external checkpoint.

## Validation boundary

Tests cover refusal duplicates/conflicts, withdrawal, replay, credentials and
peer denial, malformed messages/replies, bounds, private files, locking,
restart/rollback and persistence fault poisoning. Fixtures preserve private
file modes under umask 0002. They prohibit process launch and inspect integration
boundaries. No test here proves privileged source trust, browser egress safety,
runtime identity or real containment. This constrained implementation session
uses static checks only; tests have been authored but not executed.
