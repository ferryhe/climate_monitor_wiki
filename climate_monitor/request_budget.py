"""One durable run ledger for governed target reservations and native tool dispatch.

A target reservation is spent even if transport fails or the worker crashes.
It is not proof of an HTTP response. Policy/unsupported/precheck events spend no
fetch unit. Native fetch-tool units are distinct from governed target sends.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from contextlib import contextmanager

DEFAULT_SEARCH_RESULTS_PER_CALL = 10
DEFAULT_SEARCH_ATTEMPTS = 36
DEFAULT_SEARCH_RESULTS = DEFAULT_SEARCH_ATTEMPTS * DEFAULT_SEARCH_RESULTS_PER_CALL
DEFAULT_FETCH_ATTEMPTS = 5000
V2_AGENT_PROTOCOL_VERSION = "trusted-search-ledger.v2"
AGENT_PROTOCOL_VERSION = "trusted-candidate-handles.v3"
PROVIDER_NATIVE_SEARCH_POLICY = "provider-native-unbounded.v1"
CANDIDATE_RECEIPT_POLICY = "trusted-tool-receipts.v1"
SEARCH_IDENTITY_TAG = "climate_trusted_search_identity_v1"
CANDIDATE_HANDLE_TAG = "climate_trusted_candidate_handles_v1"


def candidate_handle_protocol(binding):
    return binding.get("agent_protocol") == {
        "version": AGENT_PROTOCOL_VERSION,
        "search_policy": PROVIDER_NATIVE_SEARCH_POLICY,
        "candidate_policy": CANDIDATE_RECEIPT_POLICY,
    }


def provider_native_unbounded_search(binding):
    protocol = binding.get("agent_protocol")
    return protocol == {
        "version": V2_AGENT_PROTOCOL_VERSION,
        "search_policy": PROVIDER_NATIVE_SEARCH_POLICY,
    } or candidate_handle_protocol(binding)


def candidate_handle_suffix(handles):
    identity = json.dumps(
        {"result_handles": handles}, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"),
    ).replace("<", "\\u003c").replace(">", "\\u003e")
    return (
        f"\n\n<{CANDIDATE_HANDLE_TAG}>{identity}</{CANDIDATE_HANDLE_TAG}>\n"
        "Trusted acquisition instruction: use climate_stage_candidate with one "
        "of these ordered result handles. The tool owns URL, search identity, "
        "publication-date checks, and controlled body evidence."
    )


def original_candidate_search_result(result, handles):
    if not isinstance(result, str):
        return result
    suffix = candidate_handle_suffix(handles)
    return result[:-len(suffix)] if result.endswith(suffix) else result


def search_identity_suffix(tool_call_id, query):
    identity = json.dumps(
        {"query": query, "tool_call_id": tool_call_id},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).replace("<", "\\u003c").replace(">", "\\u003e")
    return (
        f"\n\n<{SEARCH_IDENTITY_TAG}>{identity}</{SEARCH_IDENTITY_TAG}>\n"
        "Trusted acquisition instruction: candidates copied from this completed "
        "web_search must use discovery_kind=\"search\" and copy this exact raw "
        "tool_call_id as discovery_search_ref. Copy discovery_ref and url only "
        "from a data.web[].url in the original result above; never reuse this ID "
        "for another search or invent, remap, or change a URL."
    )


def original_search_tool_result(result, tool_call_id, query):
    if not all(isinstance(value, str) for value in (result, tool_call_id, query)):
        return result
    suffix = search_identity_suffix(tool_call_id, query)
    return result[:-len(suffix)] if result.endswith(suffix) else result


def _empty_systemic_read_failure():
    return {"signature": None, "count": 0, "root_error": None, "stopped": False}


class RequestBudgetError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def candidate_search_ref(ledger_identity, attempt, session_id, tool_call_id):
    """Return the opaque public search identity for one durable v3 call."""
    if type(attempt) is not int or attempt < 1 or not all(
        isinstance(value, str) and value.strip()
        for value in (ledger_identity, session_id, tool_call_id)
    ):
        raise ValueError("candidate search identity is invalid")
    return "search-" + digest({
        "ledger": ledger_identity, "attempt": attempt,
        "session_id": session_id.strip(), "tool_call_id": tool_call_id.strip(),
    })


def ledger_path(binding):
    return Path(binding["checkpoint_dir"]).parent / "request-budget.json"


class RequestBudget:
    def __init__(self, path, binding, *, prior=None):
        self.path = Path(path)
        self.attempt = int(binding["attempt"])
        identity_fields = {
            key: binding.get(key) for key in (
            "run_id", "effective_sha256", "budgets", "source_inventory",
            "site_scope_inventory", "governed_gateway", "date_policy", "report_date",
            )
        }
        # Preserve the exact pre-v2 digest for already-frozen legacy runs.
        if "agent_protocol" in binding:
            identity_fields["agent_protocol"] = binding["agent_protocol"]
        self.identity = digest(identity_fields)
        self.limits = dict(binding["budgets"])
        self.provider_native_search = provider_native_unbounded_search(binding)
        self.candidate_handles = candidate_handle_protocol(binding)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked(create=True) as state:
            now = time.time()
            if not state:
                if self.attempt != 1:
                    raise ValueError("durable request ledger missing on resume")
                state.update(identity=self.identity, limits=self.limits, active=1,
                             attempts={}, events=[], receipts={}, reads={}, operations=[],
                             prior=dict(prior or {}), last_seen=now,
                             systemic_read_failure=_empty_systemic_read_failure())
                if self.candidate_handles:
                    state.update(result_handles={}, candidate_receipts={})
            self._validate(state)
            if self.attempt != state["active"]:
                if self.attempt != state["active"] + 1:
                    raise ValueError("request ledger attempt identity is not sequential")
                previous = state["attempts"].get(str(state["active"]))
                previous_was_finished = bool(
                    previous and previous["finished"] is not None
                )
                self._finish(state, state["active"], now)
                if previous_was_finished:
                    state["systemic_read_failure"] = _empty_systemic_read_failure()
                state["active"] = self.attempt
            key = str(self.attempt)
            if key not in state["attempts"]:
                spent = self._runtime(state, now)
                state["attempts"][key] = {"started": now, "finished": None,
                                         "deadline": now + max(0, self.limits["runtime_seconds"] - spent)}

    @contextmanager
    def _locked(self, *, create=False, write=True):
        with self.path.with_suffix(".lock").open("a+b") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists():
                state = json.loads(self.path.read_text())
                expected = state.pop("sha256", None)
                if expected != digest(state):
                    raise ValueError("request ledger digest differs")
                self._validate(state)
                state.setdefault("systemic_read_failure", _empty_systemic_read_failure())
                if self.candidate_handles:
                    state.setdefault("result_handles", {})
                    state.setdefault("candidate_receipts", {})
            elif create:
                lock.seek(0)
                if lock.read():
                    raise ValueError("durable request ledger missing after initialization")
                lock.write(b"initialized")
                lock.flush()
                os.fsync(lock.fileno())
                state = {}
            else:
                raise ValueError("durable request ledger missing")
            try:
                yield state
            finally:
                if state and write:
                    raw = {**state, "sha256": digest(state)}
                    temporary = self.path.with_name(self.path.name + ".tmp")
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as output:
                        json.dump(raw, output, sort_keys=True, ensure_ascii=False)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, self.path)
                    directory = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)

    def _validate(self, state):
        if state["identity"] != self.identity or state["limits"] != self.limits:
            raise ValueError("immutable request ledger identity/limits differ")
        systemic = state.get("systemic_read_failure")
        if systemic is None:
            return
        if (
            set(systemic) != {"signature", "count", "root_error", "stopped"}
            or type(systemic["count"]) is not int
            or systemic["count"] < 0
            or type(systemic["stopped"]) is not bool
            or (systemic["signature"] is not None
                and (not isinstance(systemic["signature"], str) or not systemic["signature"]))
            or (systemic["root_error"] is not None
                and (not isinstance(systemic["root_error"], str) or not systemic["root_error"]))
            or (systemic["signature"] is None
                and (systemic["count"] != 0 or systemic["root_error"] is not None
                     or systemic["stopped"]))
            or (systemic["signature"] is not None
                and (systemic["count"] == 0 or systemic["root_error"] is None))
        ):
            raise ValueError("durable systemic read-failure state is invalid")

    def _runtime(self, state, now):
        return float(state["prior"].get("runtime_seconds", 0)) + sum(
            max(0, (row["finished"] if row["finished"] is not None else now) - row["started"])
            for row in state["attempts"].values())

    def _check_time(self, state):
        now = time.time()
        row = state["attempts"][str(self.attempt)]
        if (state.get("fault") or state["active"] != self.attempt or row["finished"] is not None
                or now < state["last_seen"] or now >= row["deadline"]):
            raise RequestBudgetError("runtime/attempt precheck blocked request")
        state["last_seen"] = now
        return row["deadline"] - now

    def remaining_seconds(self):
        with self._locked() as state:
            return self._check_time(state)

    def _usage(self, state, attempt=None):
        prior = state["prior"] if attempt is None else {}
        events = [e for e in state["events"] if attempt is None or e["attempt"] == attempt]
        targets = sum(e.get("fetch_units", 0) for e in events if e["event_kind"] == "network")
        tools = sum(e.get("fetch_units", 0) for e in events if e["event_kind"] == "tool")
        retries = {}
        seen_operations = set()
        seen_keys = set()
        for event in state["events"]:
            operation, key = event.get("operation"), event.get("retry_key")
            if not event.get("fetch_units") or operation in seen_operations:
                continue
            if key in seen_keys and (attempt is None or event["attempt"] == attempt):
                retries[key] = retries.get(key, 0) + 1
            seen_keys.add(key)
            seen_operations.add(operation)
        return {
            "fetch_attempts": int(prior.get("fetch_attempts", 0)) + targets + tools,
            "search_attempts": int(prior.get("search_attempts", 0)) + sum(e.get("search_units", 0) for e in events),
            "search_results": int(prior.get("search_results", 0)) + sum(e.get("result_count", 0) for e in events),
            "search_results_reserved": sum(e.get("result_reservation", 0) for e in events),
            "target_send_reservations": targets, "fetch_tool_units": tools,
            "retries_per_item": retries, "retries": sum(retries.values()),
            "runtime_seconds": self._runtime(state, time.time()) if attempt is None else max(
                0, (state["attempts"][str(attempt)]["finished"] or time.time()) - state["attempts"][str(attempt)]["started"]),
        }

    def usage(self, attempt=None):
        with self._locked() as state:
            return self._usage(state, attempt)

    def events(self, attempt=None):
        with self._locked() as state:
            return copy.deepcopy([e for e in state["events"] if attempt is None or e["attempt"] == attempt])

    def tool_event(self, call_id):
        """Read one exact durable tool event without rewriting the ledger."""
        with self._locked(write=False) as state:
            matching = [
                event for event in state["events"]
                if event.get("call_id") == call_id and event["event_kind"] == "tool"
            ]
            if len(matching) != 1:
                return None
            return copy.deepcopy(matching[0])

    def register_search_result_handles(self, session_id, tool_call_id, result):
        """Mint v3 handles only for rows in one completed current-attempt search."""
        if not self.candidate_handles:
            raise ValueError("search result handles require the frozen v3 protocol")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (session_id, tool_call_id)
        ):
            raise ValueError("search result handles require a bound native search")
        from scripts.run_agent_acquisition import _event_search_result_rows
        rows = _event_search_result_rows(result)
        call_id = f"{self.attempt}:{session_id.strip()}:{tool_call_id.strip()}"
        with self._locked() as state:
            matching = [
                event for event in state["events"]
                if event.get("call_id") == call_id and event["event_kind"] == "tool"
            ]
            if len(matching) != 1:
                raise ValueError("search result handle lacks one durable tool event")
            event = matching[0]
            if any((
                event.get("tool") != "web_search",
                event.get("completed") is not True,
                event.get("status") != "ok",
                event.get("result") != result,
            )):
                raise ValueError("search result handle requires exact successful completion")
            event["session_id"] = session_id.strip()
            event["tool_call_id"] = tool_call_id.strip()
            event["search_ref"] = candidate_search_ref(
                self.identity, self.attempt, session_id, tool_call_id,
            )
            minted = []
            for ordinal, row in enumerate(rows):
                url = row["url"]
                record = {
                    "attempt": self.attempt,
                    "session_id": session_id.strip(),
                    "tool_call_id": tool_call_id.strip(),
                    "ordinal": ordinal,
                    "url": url,
                    "canonical_url": row["canonical_url"],
                    "result_row": row["row"],
                    "result_row_sha256": digest(row["row"]),
                    "result_sha256": digest(result),
                    "query": event.get("url"),
                    "attempted_at": event.get("attempted_at"),
                    "discovery_kind": "search",
                    "search_ref": event["search_ref"],
                }
                handle = "result-" + digest({
                    "ledger": self.identity, **record,
                })
                record["handle"] = handle
                current = state["result_handles"].get(handle)
                if current is not None and current != record:
                    raise ValueError("search result handle collision")
                state["result_handles"][handle] = record
                minted.append(copy.deepcopy(record))
            return minted

    def result_handles(self):
        if not self.candidate_handles:
            raise ValueError("candidate result handles require the frozen v3 protocol")
        with self._locked(write=False) as state:
            rows = list(state["result_handles"].values())
            return copy.deepcopy(sorted(
                rows, key=lambda row: (
                    row["attempt"], str(row["session_id"] or ""),
                    str(row["tool_call_id"] or ""),
                    row["ordinal"], row["handle"],
                ),
            ))

    def completed_search_events(self):
        """Return cumulative v3 search truth from the locked durable ledger."""
        if not self.candidate_handles:
            raise ValueError("cumulative search events require the frozen v3 protocol")
        with self._locked(write=False) as state:
            events = []
            for event in state["events"]:
                if event.get("event_kind") != "tool" or event.get("tool") != "web_search":
                    continue
                if event.get("completed") is not True:
                    continue
                session_id = event.get("session_id")
                tool_call_id = event.get("tool_call_id")
                expected_ref = candidate_search_ref(
                    self.identity, int(event["attempt"]), session_id, tool_call_id,
                )
                if event.get("search_ref") != expected_ref:
                    raise ValueError("durable v3 search identity differs")
                events.append({
                    "attempt": int(event["attempt"]),
                    "session_id": session_id,
                    "tool_call_id": tool_call_id,
                    "search_ref": expected_ref,
                    "tool": "web_search",
                    "arguments": copy.deepcopy(event.get("search_arguments")),
                    "result": copy.deepcopy(event.get("result")),
                    "durable_status": event.get("status"),
                    "attempted_at": event.get("attempted_at"),
                })
            return copy.deepcopy(sorted(
                events, key=lambda row: (
                    row["attempt"], row["attempted_at"], row["session_id"],
                    row["tool_call_id"],
                ),
            ))

    def register_site_candidate_handles(self, candidates):
        """Bind frozen governed-site history to attempt-local v3 handles."""
        if not self.candidate_handles or not isinstance(candidates, list):
            raise ValueError("site candidate handles require the frozen v3 protocol")
        with self._locked() as state:
            minted = []
            for ordinal, candidate in enumerate(candidates):
                if not isinstance(candidate, dict):
                    raise ValueError("controlled site candidate must be an object")
                url = candidate.get("url")
                source_key = candidate.get("source")
                discovery_ref = candidate.get("discovery_ref")
                if not all(isinstance(value, str) and value.strip()
                           for value in (url, source_key, discovery_ref)):
                    raise ValueError("controlled site candidate identity is invalid")
                from climate_monitor.dedupe import canonical_url
                record = {
                    "attempt": self.attempt, "session_id": None,
                    "tool_call_id": None, "ordinal": ordinal,
                    "url": url, "canonical_url": canonical_url(url),
                    "result_row": copy.deepcopy(candidate),
                    "result_row_sha256": digest(candidate),
                    "result_sha256": digest(candidates),
                    "query": None,
                    "attempted_at": candidate.get("observed_at") or time.time(),
                    "source_key": source_key,
                    "discovery_ref": discovery_ref,
                    "discovery_kind": "site",
                }
                handle = "site-result-" + digest({
                    "ledger": self.identity, **record,
                })
                record["handle"] = handle
                current = state["result_handles"].get(handle)
                if current is not None and current != record:
                    raise ValueError("site candidate handle collision")
                state["result_handles"][handle] = record
                minted.append(copy.deepcopy(record))
            return minted

    def result_handle(self, handle, *, session_id):
        if not self.candidate_handles:
            raise ValueError("candidate result handles require the frozen v3 protocol")
        if not isinstance(handle, str) or not isinstance(session_id, str):
            raise ValueError("candidate result handle identity is invalid")
        with self._locked(write=False) as state:
            row = state["result_handles"].get(handle)
            site_handle = (
                row is not None and row.get("discovery_kind") == "site"
                and row.get("session_id") is None
            )
            if (
                row is None or row.get("attempt") != self.attempt
                or (not site_handle and row.get("session_id") != session_id)
            ):
                raise ValueError("candidate result handle is not bound to this attempt/session")
            return copy.deepcopy(row)

    def begin_candidate_stage(
        self, result_handle, *, session_id, source_key, date_binding=None,
    ):
        """Reserve one candidate stage, reusing verified successes without a resend."""
        if not self.candidate_handles:
            raise ValueError("candidate staging requires the frozen v3 protocol")
        with self._locked() as state:
            row = state["result_handles"].get(result_handle)
            if row is None or row.get("attempt") != self.attempt:
                raise ValueError("candidate result handle is not bound to this attempt/session")
            if row.get("source_key") is not None and row["source_key"] != source_key:
                raise ValueError("candidate result handle source identity differs")
            if row.get("session_id") is None and row.get("discovery_kind") == "site":
                row["session_id"] = session_id
            elif row.get("session_id") != session_id:
                raise ValueError("candidate result handle is not bound to this attempt/session")
            existing = state["candidate_receipts"].get(result_handle)
            if existing and existing.get("state") in {"staged", "finalized"}:
                if existing.get("source_key") != source_key:
                    raise ValueError("candidate receipt source identity differs")
                if digest(existing["payload"]) != existing["sha256"]:
                    raise ValueError("candidate receipt hash differs")
                return {"reuse": True, "receipt": copy.deepcopy(existing["payload"])}
            if existing and existing.get("state") == "in_progress":
                raise RequestBudgetError("candidate stage is already in progress")
            if isinstance(date_binding, dict):
                for other_handle, prior in state["candidate_receipts"].items():
                    if prior.get("state") not in {"staged", "finalized"}:
                        continue
                    prior_result = state["result_handles"].get(other_handle)
                    prior_item = prior.get("payload", {}).get("item", {})
                    prior_date_binding = {
                        "date_status": prior.get("payload", {}).get("date_status"),
                        "published_date": prior_item.get("published_date"),
                        "publication_date_evidence": prior_item.get(
                            "publication_date_evidence"
                        ),
                    }
                    if (
                        prior_result
                        and prior_result.get("canonical_url") == row.get("canonical_url")
                        and prior.get("source_key") == source_key
                        and prior_date_binding == date_binding
                    ):
                        if digest(prior["payload"]) != prior["sha256"]:
                            raise ValueError("candidate receipt hash differs")
                        alias = copy.deepcopy(prior["payload"])
                        alias.update({
                            "status": "reused",
                            "result_handle": result_handle,
                            "reused_result_handle": other_handle,
                            "attempt": self.attempt,
                        })
                        state["candidate_receipts"][result_handle] = {
                            "state": "superseded", "source_key": source_key,
                            "payload": alias, "sha256": digest(alias),
                        }
                        return {
                            "reuse": True, "receipt": copy.deepcopy(alias),
                        }
            for other_handle, prior in state["candidate_receipts"].items():
                prior_result = state["result_handles"].get(other_handle)
                if (
                    prior.get("state") == "in_progress"
                    and prior_result
                    and prior_result.get("canonical_url") == row.get("canonical_url")
                    and prior.get("source_key") == source_key
                ):
                    if prior_result.get("attempt", self.attempt) >= self.attempt:
                        raise RequestBudgetError("candidate stage is already in progress")
                    prior["state"] = "superseded"
                    prior["payload"] = {
                        "status": "superseded",
                        "reason": "interrupted candidate stage was retried by a later attempt",
                    }
                    prior["sha256"] = digest(prior["payload"])
            token = uuid.uuid4().hex
            state["candidate_receipts"][result_handle] = {
                "state": "in_progress", "token": token, "source_key": source_key,
                "attempt": self.attempt,
                "payload": {}, "sha256": digest({}),
            }
            return {"reuse": False, "token": token, "result": copy.deepcopy(row)}

    def reusable_candidate_body(self, result_handle, *, source_key):
        """Return only hash-bound governed body evidence for the same URL."""
        if not self.candidate_handles:
            raise ValueError("candidate body reuse requires the frozen v3 protocol")
        with self._locked(write=False) as state:
            result = state["result_handles"].get(result_handle)
            if result is None or result.get("attempt") != self.attempt:
                raise ValueError("candidate result handle is not bound to this attempt")
            for other_handle, prior in state["candidate_receipts"].items():
                if other_handle == result_handle or prior.get("state") not in {
                    "staged", "finalized",
                }:
                    continue
                prior_result = state["result_handles"].get(other_handle)
                payload = prior.get("payload", {})
                item = payload.get("item", {})
                evidence = item.get("evidence", {})
                controlled = payload.get("controlled_events")
                if (
                    prior_result
                    and prior_result.get("canonical_url")
                    == result.get("canonical_url")
                    and prior.get("source_key") == source_key
                    and item.get("processing_status") == "complete"
                    and evidence.get("status") == "ok"
                    and evidence.get("classification") == "full_content"
                    and isinstance(controlled, list)
                ):
                    if digest(payload) != prior["sha256"]:
                        raise ValueError("candidate receipt hash differs")
                    return {
                        "evidence": copy.deepcopy(evidence),
                        "controlled_events": copy.deepcopy(controlled),
                    }
            return None

    def complete_candidate_stage(self, result_handle, token, payload):
        if not self.candidate_handles:
            raise ValueError("candidate staging requires the frozen v3 protocol")
        with self._locked() as state:
            current = state["candidate_receipts"].get(result_handle)
            if not current or current.get("state") != "in_progress" or current.get("token") != token:
                raise ValueError("candidate stage reservation differs")
            candidate_handle = "candidate-" + digest({
                "ledger": self.identity, "result_handle": result_handle,
                "source_key": current["source_key"],
            })
            stored = copy.deepcopy(payload)
            stored.update({
                "candidate_handle": candidate_handle,
                "result_handle": result_handle,
                "source_key": current["source_key"],
                "attempt": self.attempt,
            })
            current_result = state["result_handles"][result_handle]
            competing = []
            for other_handle, prior in state["candidate_receipts"].items():
                if other_handle == result_handle or prior.get("state") not in {
                    "staged", "finalized",
                }:
                    continue
                prior_result = state["result_handles"].get(other_handle)
                if (
                    prior_result
                    and prior_result.get("canonical_url")
                    == current_result.get("canonical_url")
                    and prior.get("source_key") == current["source_key"]
                ):
                    competing.append((other_handle, prior))
            rank = {
                "unknown_pending_review": 0, "outside_window": 1,
                "eligible_unknown": 2, "eligible": 3,
            }
            current_rank = rank.get(stored.get("date_status"), -1)
            best_prior_rank = max(
                (rank.get(prior.get("payload", {}).get("date_status"), -1)
                 for _handle, prior in competing),
                default=-1,
            )
            state_name = "staged"
            if competing and current_rank <= best_prior_rank:
                state_name = "superseded"
                stored.update(
                    status="deduped", candidate_handle=None,
                    dedupe_reason="a same-URL receipt has an equal or stronger date decision",
                )
            elif competing:
                for _other_handle, prior in competing:
                    prior["state"] = "superseded"
            state["candidate_receipts"][result_handle] = {
                "state": state_name, "source_key": current["source_key"],
                "payload": stored, "sha256": digest(stored),
            }
            return copy.deepcopy(stored)

    def fail_candidate_stage(self, result_handle, token, payload):
        if not self.candidate_handles:
            raise ValueError("candidate staging requires the frozen v3 protocol")
        with self._locked() as state:
            current = state["candidate_receipts"].get(result_handle)
            if not current or current.get("state") != "in_progress" or current.get("token") != token:
                raise ValueError("candidate stage reservation differs")
            stored = copy.deepcopy(payload)
            stored.update({
                "result_handle": result_handle,
                "source_key": current["source_key"],
                "attempt": self.attempt,
            })
            state["candidate_receipts"][result_handle] = {
                "state": "failed", "source_key": current["source_key"],
                "payload": stored, "sha256": digest(stored),
            }
            return copy.deepcopy(stored)

    def finalize_candidate(self, candidate_handle, *, session_id, annotations):
        if not self.candidate_handles:
            raise ValueError("candidate finalization requires the frozen v3 protocol")
        with self._locked() as state:
            matches = [
                (key, value) for key, value in state["candidate_receipts"].items()
                if value.get("state") in {"staged", "finalized"}
                and value.get("payload", {}).get("candidate_handle") == candidate_handle
            ]
            if len(matches) != 1:
                raise ValueError("candidate handle is not one staged receipt")
            key, current = matches[0]
            result = state["result_handles"].get(key)
            if (
                result is None
                or result.get("attempt", self.attempt) > self.attempt
                or (
                    result.get("attempt") == self.attempt
                    and result.get("session_id") != session_id
                )
            ):
                raise ValueError("candidate handle is not bound to this attempt/session")
            if digest(current["payload"]) != current["sha256"]:
                raise ValueError("candidate receipt hash differs")
            if current["state"] == "finalized":
                if current["payload"].get("annotations") != annotations:
                    raise ValueError("candidate finalization differs")
                return copy.deepcopy(current["payload"])
            payload = {**current["payload"], "annotations": copy.deepcopy(annotations)}
            state["candidate_receipts"][key] = {
                **current, "state": "finalized", "payload": payload,
                "sha256": digest(payload),
            }
            return copy.deepcopy(payload)

    def candidate_receipts(self):
        if not self.candidate_handles:
            raise ValueError("candidate receipts require the frozen v3 protocol")
        with self._locked(write=False) as state:
            values = []
            for result_handle, value in state["candidate_receipts"].items():
                if digest(value["payload"]) != value["sha256"]:
                    raise ValueError("candidate receipt hash differs")
                values.append({
                    "state": value["state"], "result_handle": result_handle,
                    "source_key": value.get("source_key"),
                    **copy.deepcopy(value["payload"]),
                })
            return sorted(values, key=lambda row: row.get("result_handle", ""))

    def systemic_read_failure(self):
        with self._locked() as state:
            return copy.deepcopy(state["systemic_read_failure"])

    def reset_systemic_read_failure(self):
        with self._locked() as state:
            state["systemic_read_failure"] = _empty_systemic_read_failure()
            return copy.deepcopy(state["systemic_read_failure"])

    def record_systemic_read_failure(self, signature, *, threshold):
        if not isinstance(signature, str) or not signature or type(threshold) is not int or threshold < 1:
            raise ValueError("systemic read-failure update is invalid")
        with self._locked() as state:
            current = state["systemic_read_failure"]
            if signature == current["signature"]:
                current["count"] += 1
            else:
                current.update(signature=signature, count=1, root_error=signature, stopped=False)
            if current["count"] >= threshold:
                current["stopped"] = True
            return copy.deepcopy(current)

    def note(self, kind, url, reason, *, tool=None, call_id=None):
        with self._locked() as state:
            state["events"].append({"id": uuid.uuid4().hex, "attempt": self.attempt,
                "event_kind": kind, "url": url, "tool": tool, "call_id": call_id,
                "reason": str(reason),
                "attempted_at": time.time(), "fetch_units": 0, "search_units": 0})

    def claim(self, kind, url, *, operation=None, call_id=None, results=0, units=1,
              retry_key=None, session_id=None, tool_call_id=None,
              tool_arguments=None):
        with self._locked() as state:
            try:
                self._check_time(state)
                usage = self._usage(state)
                search = kind == "web_search"
                if search:
                    if not self.provider_native_search:
                        if usage["search_attempts"] >= self.limits["search_attempts"]:
                            raise RequestBudgetError("search budget precheck blocked request")
                        if usage["search_results"] + usage["search_results_reserved"] + results > self.limits["search_results"]:
                            raise RequestBudgetError("search result budget precheck blocked request")
                elif units < 1 or usage["fetch_attempts"] + units > self.limits["fetch_attempts"]:
                    raise RequestBudgetError("fetch budget precheck blocked request")
                if call_id and any(e.get("call_id") == call_id for e in state["events"]):
                    raise RequestBudgetError("duplicate tool dispatch blocked")
                operation = operation or uuid.uuid4().hex
                key = retry_key or url
                if not search and operation not in state["operations"]:
                    if state["reads"].get(key, 0) >= 1 + self.limits["retries_per_item"]:
                        raise RequestBudgetError("per-item retry budget precheck blocked request")
                    state["reads"][key] = state["reads"].get(key, 0) + 1
                    state["operations"].append(operation)
                event_id = uuid.uuid4().hex
                state["events"].append({"id": event_id, "attempt": self.attempt,
                    "event_kind": "network" if kind == "http" else "tool", "tool": kind,
                    "url": url, "call_id": call_id, "operation": operation, "retry_key": key,
                    "attempted_at": time.time(), "status": "reserved_or_uncertain",
                    "fetch_units": 0 if search else units, "search_units": int(search),
                    "result_reservation": results if search else 0, "result_count": 0})
                if self.candidate_handles and search:
                    event = state["events"][-1]
                    event["session_id"] = session_id
                    event["tool_call_id"] = tool_call_id
                    event["search_arguments"] = copy.deepcopy(
                        tool_arguments if isinstance(tool_arguments, dict)
                        else {"query": url}
                    )
                    if event["search_arguments"].get("query") != url:
                        raise ValueError("candidate search arguments differ from target")
                    event["search_ref"] = candidate_search_ref(
                        self.identity, self.attempt, session_id, tool_call_id,
                    )
                return event_id
            except RequestBudgetError as exc:
                state["events"].append({"id": uuid.uuid4().hex, "attempt": self.attempt,
                    "event_kind": "precheck", "tool": kind, "url": url, "call_id": call_id,
                    "attempted_at": time.time(), "reason": str(exc)})
                raise

    def complete_tool(self, call_id, result, status):
        with self._locked() as state:
            matching = [e for e in state["events"] if e.get("call_id") == call_id and e["event_kind"] == "tool"]
            if not matching:
                return
            event = matching[0]
            if event.get("completed"):
                if event.get("result") != result or event["status"] != status:
                    raise ValueError("tool completion differs from durable result")
                return
            if event["tool"] == "web_search":
                # Reuse the transcript URL extraction contract, not model claims.
                from scripts.run_agent_acquisition import _event_result_urls
                count = len(_event_result_urls(result)) if status == "ok" else 0
                if not self.provider_native_search and count > event["result_reservation"]:
                    state["fault"] = "search provider exceeded its reserved result limit"
                    raise ValueError(state["fault"])
                event["result_count"] = count
                event["result_reservation"] = 0
            event["status"] = status
            event["result"] = result
            event["completed"] = True

    def save_receipt(self, key, payload):
        with self._locked() as state:
            state["receipts"][key] = {"payload": payload, "sha256": digest(payload)}

    def receipt(self, key, *, reset_systemic=False):
        with self._locked() as state:
            value = state["receipts"].get(key)
            if value is None:
                return None
            if digest(value["payload"]) != value["sha256"]:
                raise ValueError("seed receipt hash differs")
            if reset_systemic and value["payload"].get("status") in {"success", "rejected"}:
                state["systemic_read_failure"] = _empty_systemic_read_failure()
            return copy.deepcopy(value["payload"])

    def _finish(self, state, attempt, now):
        row = state["attempts"].get(str(attempt))
        if row and row["finished"] is None:
            row["finished"] = max(row["started"], now)

    def finish(self):
        with self._locked() as state:
            self._finish(state, self.attempt, time.time())


class GuardedGateway:
    """Delegate all policy/transport work; attach only the public target hook."""
    def __init__(self, gateway, budget, lane, *, transport_timeout_seconds):
        self.gateway, self.budget, self.lane = gateway, budget, lane
        self.transport_timeout_seconds = float(transport_timeout_seconds)

    def __getattr__(self, name):
        return getattr(self.gateway, name)

    def read(self, url, **kwargs):
        operation = uuid.uuid4().hex
        existing = kwargs.pop("before_target_request", None)
        def before(target, decision):
            self.budget.claim("http", target, operation=operation, retry_key=f"{self.lane}:{url}")
            return existing(target, decision) if existing else None
        remaining = self.budget.remaining_seconds()
        raw_timeout = kwargs.get("timeout_seconds", self.transport_timeout_seconds)
        requested = (
            self.transport_timeout_seconds
            if raw_timeout is None
            else float(raw_timeout)
        )
        kwargs["timeout_seconds"] = min(
            requested, self.transport_timeout_seconds, remaining,
        )
        return self.gateway.read(url, before_target_request=before, **kwargs)


def hook_decision(budget, payload):
    tool = payload.get("tool_name")
    recorded_tool = str(tool or "unknown")
    args = payload.get("tool_input") or {}
    urls = args.get("urls") if isinstance(args.get("urls"), list) else []
    url = str(args.get("url") or (urls[0] if urls else args.get("query") or recorded_tool))
    extra = payload.get("extra") or {}
    call, session = extra.get("tool_call_id"), payload.get("session_id")
    valid_identity = all(isinstance(value, str) and value.strip() for value in (session, call))
    call_id = f"{budget.attempt}:{session.strip()}:{call.strip()}" if valid_identity else None
    if tool in {"climate_stage_candidate", "climate_finalize_candidate"}:
        if budget.candidate_handles and valid_identity:
            return {}
        reason = "unconfigured acquisition tool" if not budget.provider_native_search else "missing durable session/tool-call identity"
        budget.note("precheck", url, reason, tool=recorded_tool, call_id=call_id)
        return {"action": "block", "message": reason}
    if tool not in {"web_search", "web_extract", "browser_exec"}:
        reason = "unconfigured acquisition tool"
        budget.note("precheck", url, reason, tool=recorded_tool, call_id=call_id)
        return {"action": "block", "message": reason}
    if not valid_identity:
        reason = "missing durable session/tool-call identity"
        budget.note("precheck", url, reason, tool=recorded_tool)
        return {"action": "block", "message": reason}
    if payload.get("hook_event_name") == "post_tool_call":
        result = extra.get("result")
        if budget.provider_native_search and tool == "web_search":
            result = original_search_tool_result(result, call.strip(), args.get("query"))
        budget.complete_tool(call_id, result, extra.get("status", "error"))
        return {}
    results = args.get("num_results", args.get("limit", 5)) if tool == "web_search" else 0
    supplied_result_limits = (
        [args[key] for key in ("num_results", "limit") if key in args]
        if tool == "web_search" else []
    )
    if (not budget.candidate_handles
            and any(type(value) is not int or value < 1 for value in supplied_result_limits)):
        reason = "invalid search result limit"
        budget.note("precheck", url, reason, tool=tool, call_id=call_id)
        return {"action": "block", "message": reason}
    if (not budget.provider_native_search
            and any(value > DEFAULT_SEARCH_RESULTS_PER_CALL for value in supplied_result_limits)):
        reason = (
            "search result limit exceeds per-call maximum of "
            f"{DEFAULT_SEARCH_RESULTS_PER_CALL}"
        )
        budget.note("precheck", url, reason, tool=tool, call_id=call_id)
        return {"action": "block", "message": reason}
    if budget.candidate_handles and tool == "web_search":
        # Hermes/provider schema owns request-shape validation in v3. The
        # application records only actual result cardinality after completion.
        results = 0
    try:
        budget.claim(
            tool, url, call_id=call_id, results=results,
            units=max(1, len(urls)), retry_key=f"{tool}:{url}",
            session_id=session.strip() if budget.candidate_handles and tool == "web_search" else None,
            tool_call_id=call.strip() if budget.candidate_handles and tool == "web_search" else None,
            tool_arguments=args if budget.candidate_handles and tool == "web_search" else None,
        )
        return {}
    except (RequestBudgetError, ValueError) as exc:
        return {"action": "block", "message": str(exc)}
