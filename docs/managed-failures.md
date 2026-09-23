# Managed execution failures

Acquisition, report authoring (including its capability probe), meeting extraction,
and frozen-runtime probes use `climate_monitor.managed_runtime`.
The additive `failure` field uses `climate-managed-failure.v1`; existing result
schemas and successful payloads are unchanged. Meeting SQLite rows retain this
receipt in the existing error text column and expose it as a structured field.

| Category | Retryable | Operator action |
| --- | --- | --- |
| `transient_service` | Only with verified cleanup | Restore service, then explicitly resume |
| `timeout_cancelled` | No | Inspect timeout/cancellation; no automatic retry |
| `identity_drift` | No | Start a fresh run |
| `configuration_auth` | No | Correct configuration/authentication; start a fresh run |
| `frozen_input` | No | Correct the input contract; start a fresh run |
| `internal` | No | Inspect the controlled runtime |

Only a typed, controlled `ManagedFailure('transient_service')` can authorize a
retry. Generic exit codes, malformed responses, missing diagnostic receipts,
unclassified exceptions, and historical failures without a typed receipt do not.
Hermes does not expose a verified machine-readable transient error contract in
the supported CLI path, so a generic provider exit stays terminal. We do not
infer recoverability from provider stderr, model text, an exit of 75, incomplete
coverage, or an operator-supplied retry flag. No automatic retries are added. `recoverable` records verified execution quiescence
(or a failure before a child launched); it is not retry authorization. The separate
`retryable` field also requires the explicit transient category.
`cleanup=None` retains the trusted in-process no-child semantics used by failures
before launch. Serialized terminal categories stay non-retryable. A serialized
transient receipt with missing or null cleanup cannot
authorize retry: no current production path requires that exception. Non-null
cleanup must contain a safe positive PGID distinct from the receiving manager's
group, `verified=True`, and boolean TERM/KILL fields to remain verified. Malformed
receipts are normalized to bounded unverified evidence and block recovery.
Controlled frozen hooks use exit 76 for identity drift; frozen startup rejection
uses 65. Unknown exits default to `internal`.

Diagnostics contain only the schema, category, fixed message, retry/recovery
booleans, bounded exit code, and cleanup booleans/PGID. They contain no raw stderr,
stdout, command, environment, exception text, provider or model. Report response
stdout and the strict session footer are consumed internally for response
validation; they are not copied into failure/checkpoint diagnostics. Meeting
execution failures stop the batch and leave untouched items pending. A terminal
receipt cannot be retried through the batch retry selector or report checkpoint.
Batch stopping follows the classified category: recognized authentication,
configuration, identity and cancellation failures stop even when raised as raw
exceptions. Unclassified raw internal/content exceptions remain per-item;
explicit managed failures, including managed internal failures, stop the batch.

Each child starts a new POSIX session (`start_new_session=True`), with leader PID
as PGID. Managed execution requires Linux `waitid(..., WNOWAIT)`, procfs, child
subreaping and pidfds. Python builds without pidfd wrappers use the checked Linux
x86_64/aarch64 syscall ABI; unsupported runtimes fail before launching a child.

The leader is held unreaped throughout TERM/grace/KILL and descendant inspection.
`waitid(P_PID, WEXITED|WNOHANG|WNOWAIT)` observes exit without consuming ownership;
procfs start time, group and session must still match the original child. There is
no `communicate`, `poll` or `wait` before cleanup. Captured I/O and input use private
temporary regular files, preserving byte/text/encoding/newline behavior without
pipe deadlock while the leader remains held. Explicit output files are preserved.

Signals are restricted to safe positive groups, never the manager's group. Every
group signal requires the original held generation. Cleanup observes all group
members and reaps adopted non-leader zombies by their individual child PIDs. A
pass that reaps a member cannot prove absence; another scan is required. Only once
the group is reduced to the exited, held leader does `process.wait()` consume it,
eliminating the original group. No `killpg`, including a probe, occurs afterward.
Normal completion also uses this cleanup path. Cleanup errors fail closed; a
pidfd permits aborting/reaping the original leader without signaling a reused
numeric identity. No numeric group signal is authorized after ownership is lost.

SIGTERM/SIGINT request cancellation; the execution loop checks between bounded
20-millisecond sleeps and heartbeat callbacks. Cleanup records repeated cancellation without interruption and restores signal handlers
on exit. Hermes children have a 1-second grace per TERM/KILL phase. The report
worker has a 5-second grace per phase, allowing it to clean its separately grouped
Hermes child before the outer owner escalates. Non-main-thread callers retain
Python's signal-handler rules; exceptions still trigger cleanup.

Private `managed-processes/*.json` markers retain immutable audit evidence.
Creation walks normalized absolute ancestry from a held filesystem-root descriptor,
opening every component no-follow. The marker directory must be owned by the worker
and private (0700, with no group/world permission bits). `openat(O_EXCL|O_NOFOLLOW)` creates a 0600 marker; its retained FD is
written, truncated and fsynced for the launch transition from `{pgid: null}` to
`{pgid: child_pid}`. No pathname reopen or unlink is used. Completed and failed
receipts remain; the writer compares and parses the exact expected PID receipt,
checks retained name/ancestry/metadata bindings, then rereads exact bytes before
authorizing recovery. A launch-crash null PGID blocks recovery. Name replacement or
ancestry drift blocks recovery and preserves both moved and replacement files.
Failed cleanup retains frozen-auth inflight ownership and disables retry.

Automatic meeting extraction starts independently before report authoring and
owns only `<run>/<SNAPSHOT>/meetings`. Acquisition/report result, execution,
startup-owner and resume checks use `verify_acquisition_quiescent`: it validates
the fixed directory boundary with descriptor-relative, no-follow directory opens.
It retains the run, snapshot and meeting descriptors through the final parent-name
binding checks and skips only the opened meeting home. There are no configurable
exclusions. Recursive scans use directory descriptors and reject changed directory
metadata or bindings. Missing fixed components may remain absent, but their creation
during a scan blocks recovery. Meeting content changes do not invalidate the
independent acquisition/report check; replacement of its directory does.
Outer report, report Hermes, acquisition attempts and all other nested markers
remain subject to verification. Symlink, unreadable or non-directory exclusion
boundaries fail closed. The meeting worker continues to use `verify_quiescent`
on its own subtree, so its live/ambiguous markers still block meeting recovery.
Every owned `managed-processes` entry must be a readable real directory owned by
the worker with no group/world permission bits. Each receipt must also be worker-owned,
have no group/world permission bits, and have exactly one hard link. Both writer
and verifier enforce these properties. Its
entries must be regular `.json` receipts of at most 4096 bytes containing only a
safe PGID. Descriptor-relative no-follow/nonblocking reads reject symlinks, special
files, malformed receipts and replacement races. Recovery verification is read-only and retains stale receipts: POSIX unlink by
name cannot atomically verify the inode being removed. Execution owners also retain completed markers; no component unlinks them. Receipt descriptors and
metadata stay bound through the final checks, alongside directory descriptors.
Reads loop to EOF with a 4096-byte limit. Final verification rewinds, compares exact
bytes, reparses the schema/PGID and checks current group absence, then checks bytes
again after the group probe and validates final metadata/name bindings. Metadata
equality alone never establishes content equality.
The shared ancestry walk retains every component from `/`, rejects symlinks, and
revalidates component bindings. Unscanned shared ancestors use identity/mode/owner
checks; scanned directories also require unchanged content metadata. Missing names
must remain absent with unchanged parent metadata. Limits are 64 path/subtree
levels, 1024 retained descriptors, 20000 enumerated entries, 512 receipts per scan
or marker directory, and 4096 bytes per receipt. Exceeding a limit fails closed;
there is no automatic audit pruning or retry.
All directory descriptors remain open through final verification; descriptor
exhaustion or other filesystem errors block recovery. These checks detect changes
during verification, not launches after it returns; existing owner locks remain
necessary for serializing execution and recovery.

This is POSIX process-group containment, not a sandbox against descendants that
deliberately create another session. Managed report workers participate in the
same ownership protocol for their nested sessions. Host services and cgroups are
outside this change. Tests include real TERM-resistant children and grandchildren,
leader-exit races, cancellation, permission/wait failures, and unsafe PGIDs.

API references: [Python subprocess](https://docs.python.org/3.11/library/subprocess.html),
[Linux child subreapers](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html),
[POSIX group signaling on Linux](https://man7.org/linux/man-pages/man2/kill.2.html).
