# Host Hermes invocation protocol v1 (#157)

**Production readiness: false.** `climate_monitor.host_exec` is an opt-in,
stdlib-only Linux security boundary. It has no executable launcher, runner
callback, CLI, service installation, app caller, environment auto-configuration,
HTTP route or Compose mount. It cannot execute Hermes. #158 owns the actual
cgroup runner; #159 owns integration. No request can report ready, retryable,
completed, cancelled or verified cleanup in this version.

## Boundary and field provenance

An application constructs an immutable invocation, loads a dedicated credential,
and calls `make_request` then `call`. An explicitly constructed `Server` owns
`open`, repeated `serve_once`, and `close`. Both require an absolute private
directory and credential path plus an explicit expected owner UID. These are
local operator configuration, never wire fields. There are no defaults pointing
to production. `executor.sock` is distinct from `dashboard.sock`.

The invocation has exactly these fields:

| Field | Source and contract |
| --- | --- |
| `invocation_id` | Caller generates 16 random bytes as 32 lowercase hex characters once per invocation generation; retain across readback/cancel. Never regenerate after uncertainty. |
| `run_id` | Existing managed binding `run_id`; 1–128 ASCII letters, digits, underscore or hyphen. |
| `attempt` | Existing managed binding attempt, integer 1–1,000,000; booleans rejected. |
| `purpose` | Fixed `hermes-cli-invocation`, matched to credential policy. Not Dashboard, report publication or arbitrary task selection. |
| `binding_sha256` | SHA-256 of exact frozen attempt binding bytes, as used by the existing attempt seal. |
| `frozen_identity_sha256` | Existing `binding.hermes_snapshot.sha256`: digest of exact private snapshot manifest bytes, not the later effective provider/model identity. |
| `repository_commit_sha` | Existing binding's full 40-character lowercase Git commit SHA. Do not substitute a branch or hash a shortened SHA. |
| `runtime_sha256` | SHA-256 of the canonical JSON `runtime` object from the verified frozen snapshot manifest (`hermes_identity.load_snapshot`). #158 must independently verify its runtime against this commitment. |
| `timeout_seconds` | Caller requests integer 1–3,600; #158 must enforce an operator ceiling and hard deadline. |
| `output_bytes` | Caller requests integer 1–1,048,576; #158 must enforce it across actual output streams. No output is collected here. |

The server checks shapes, bindings and authentication, **not the truth of supplied
commitments**. This module never loads business bindings, snapshots, credentials
for a provider, or runtime binaries. #159 must construct these fields from the
verified existing binding/snapshot; #158 must validate host-owned runtime and
repository allowlists before launching. A digest is not proof of confinement.
No command, argv, prompt, environment, path, mount, image, UID selection or
business write operation exists in the request schema. A future runner must
resolve approved inputs using the verified identity, not reinterpret a wire
field as a path or command.

## Wire, authentication and limits

One AF_UNIX/SOCK_STREAM connection carries one request and one response. Each
is a 4-byte unsigned network-order length followed by canonical ASCII JSON:
sorted keys, compact separators, ASCII escapes, no NaN/Infinity, duplicate keys,
unknown fields or noncanonical spellings. Sender half-closes after its frame;
trailing bytes fail. Each frame is at most 8,192 bytes. Receive has a one-second
total deadline, including EOF; sending also has a one-second timeout. Backlog
is eight. There is no TCP fallback, retry or reconnect.

The exact request fields are `version` (integer 1), `audience`
(`climate-host-hermes-executor`), `key_id`, `operation` (`submit`, `readback`,
`cancel`), `invocation`, `invocation_sha256`, `nonce` (16 fresh random bytes,
32 lowercase hex), `expires_at` (UTC epoch integer, strictly future and at most
60 seconds ahead and no later than key expiry), and `mac`. Invocation digest
is SHA-256 of the canonical invocation. The MAC is HMAC-SHA256 with domain
`request\0`, over the canonical request without `mac`. Purpose is inside the
signed invocation and must equal the credential's purpose. Every operation
carries the entire immutable invocation.

The dedicated credential file has exactly `version` (1), `key_id` (1–64 ASCII
letters/digits/underscore/hyphen), `secret` (32 cryptographically random bytes
encoded as 64 lowercase hex), `audience`, `purpose`, `uid` (expected kernel peer
UID), `expires_at`, and boolean `enabled`. Operator JSON whitespace is allowed;
duplicate keys are rejected. File size is capped at 8,192 bytes. Never share a
Dashboard token or provider secret. Generate and provision secrets out of band;
none belong in git, logs, command-line arguments or diagnostics.

The server reloads this file on every request. Atomic replacement with a new
key ID and new secret immediately invalidates old requests at the next auth
check; there is no overlap window. Set `enabled=false`, expire, or remove the
file to revoke. An already authenticated exchange may finish during rotation;
revocation is not retroactive. Rotation never clears invocation or nonce state.
The client authenticates each response with domain `result\0`, and checks its
SHA-256 binding to the complete exact request, including nonce and operation.
The HMAC secret never crosses the socket.

Signed responses have exactly `version`, `request_sha256`, `result`, and `mac`.
Result has exactly `invocation_id`, `invocation_sha256`, `status`, `ready=false`,
`retryable=false`, and `cleanup={"status":"unverified"}`. The finite statuses
are `not_ready`, `conflict`, `unknown`, `replay`, and `capacity`. Invalid requests,
key/peer/path failures, and server internal errors return only the unsigned
`{"error":"rejected","version":1}` when transport permits. Client errors
are the constant `ProtocolError` with `retryable=False`; no raw exception,
stderr, secret, path or payload is logged. Unknown/invalid cleanup evidence,
success claims, missing fields, truncated replies, crashes and disconnects
all fail closed. This deliberately minimal attestation schema only expresses
**unverified**; #158 must extend/version it with actual cgroup cleanup proof.

## Durable duplicate semantics

A locked private `ledger.json` stores only invocation-ID/digest pairs and hashes
of key-ID/nonce pairs. It contains no business data, output or history archive.
Every authenticated new nonce and new submit identity is written with exclusive
0600 creation, file fsync, atomic rename and directory fsync **before** replying.
Same ID and digest returns the existing refusal; same ID and different digest
returns `conflict`. Readback/cancel of an unknown ID returns `unknown`. Cancel
of a recorded refusal remains `not_ready`; it does not assert a process was
cancelled. Exact nonce reuse returns `replay`, including after restart.

Directory flock excludes another server; a process lock serializes accepted
updates even if the embedding caller invokes `serve_once` concurrently. State
is at most 4 MiB and 4,096 entries each for invocations/nonces (a lower constructor
limit is allowed). At capacity it rejects; nothing is evicted or reset on
rotation. This intentionally finite refusal ledger is not a production queue.
A persistence error poisons the current server against further requests. Invalid
or unsafe existing state prevents startup. Missing state initializes a new
protocol-only ledger: because this version **never launches**, it cannot cause
re-execution. Before #158 can accept intent, durable initialization/recovery,
rollback/lost-ledger detection, terminal records and crash reconciliation must
be proven; missing state must never silently authorize re-execution.

## Linux filesystem and identity threat model

Every ancestor is opened with `O_DIRECTORY|O_NOFOLLOW` using directory-relative
file descriptors. Ancestors must be owned by root or the configured owner and
not group/world writable (root-owned sticky ancestors such as `/tmp` are
allowed). The final directory must be exactly 0700 and owned by the configured
UID. Socket mode is exactly 0600 with matching ownership; token and ledger must
be regular 0600, single-link files, opened no-follow/nonblocking with bounded
reads and pre/post identity checks. Symlinks, FIFOs, devices, permissive modes,
wrong owners and malformed state fail closed. Linux `/proc/self/fd` anchors
bind/connect to the checked directory even when pathname ancestors change.
Client checks socket metadata around connect and checks server `SO_PEERCRED`;
server checks the application peer UID against credential policy.

The server refuses any existing socket at startup, including stale sockets.
It only removes its own socket inode on orderly close. An operator must resolve
stale sockets after crashes; a socket's existence or successful connection is
never readiness. Same-UID processes and host root can replace private files,
read credentials, impersonate endpoints or destroy state: POSIX checks do not
isolate processes sharing a UID. HMAC with a shared secret likewise does not
protect a client against another holder of that secret. The server's state
and credential directories must remain outside untrusted write access.

**Production UID0 limitation:** the read-only inventory reported a UID0 app
container. Depending on namespace mapping, host `SO_PEERCRED` may also see UID0;
that does not authenticate this application. The protocol's same-owner private
directory policy is only locally tested. Activation requires #158/#159 to prove
an isolated credential/mount policy and the real host-visible UID mapping,
including denial to unrelated container/host processes. Do not relax private
modes, use a group/world-writable socket, mount the state directory into the app,
or treat UID0 as sufficient. No production mount policy is supplied here.

Static review at base `9e517497093d4bae7ddbff8dcb6b088a51cb92e0`: declared
Compose files have no Docker socket, privileged setting or added capabilities.
Caddy proxies the app, and this module has no app/router import or caller.
`docker-compose.host-hermes.yml` remains a separate full-management Dashboard
relay. It is not executor configuration. `/hermes` and browser routes do not
expose this module. No production service or host state was changed.

## Verification and outstanding gates

Run focused tests with `TMPDIR` in the worktree; they use synthetic credentials
and the real Unix socket server, including malformed frames, unsafe paths,
rotation/revocation, peer mismatch, replay across restart, concurrent conflicts,
failed persistence and invalid/truncated responses. Command launch functions
are forbidden in protocol tests. Tests may need a context with real Linux UID
metadata: a filesystem sandbox that rewrites ancestor ownership is correctly
rejected, not a reason to weaken ownership checks.

#158: real Hermes CLI containment, complete cgroup descendant cleanup,
independently verified runtime/limits, durable accepted intent and crash recovery,
attestation evidence and host privilege/service tests. #159: immutable field
construction, isolated credentials/mounts, invocation recovery with no automatic
retry, application integration and canary. Independent review, Python 3.12 and
CI remain validation gates when unavailable locally. No production activation
is authorized by local protocol tests.
