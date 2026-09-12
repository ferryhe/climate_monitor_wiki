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
AGENT_PROTOCOL_VERSION = "trusted-search-ledger.v2"
PROVIDER_NATIVE_SEARCH_POLICY = "provider-native-unbounded.v1"


def provider_native_unbounded_search(binding):
    return binding.get("agent_protocol") == {
        "version": AGENT_PROTOCOL_VERSION,
        "search_policy": PROVIDER_NATIVE_SEARCH_POLICY,
    }


def _empty_systemic_read_failure():
    return {"signature": None, "count": 0, "root_error": None, "stopped": False}


class RequestBudgetError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


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
    def _locked(self, *, create=False):
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
                if state:
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

    def claim(self, kind, url, *, operation=None, call_id=None, results=0, units=1, retry_key=None):
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
    if tool not in {"web_search", "web_extract", "browser_exec"}:
        reason = "unconfigured acquisition tool"
        budget.note("precheck", url, reason, tool=recorded_tool, call_id=call_id)
        return {"action": "block", "message": reason}
    if not valid_identity:
        reason = "missing durable session/tool-call identity"
        budget.note("precheck", url, reason, tool=recorded_tool)
        return {"action": "block", "message": reason}
    if payload.get("hook_event_name") == "post_tool_call":
        budget.complete_tool(call_id, extra.get("result"), extra.get("status", "error"))
        return {}
    results = args.get("num_results", args.get("limit", 5)) if tool == "web_search" else 0
    supplied_result_limits = (
        [args[key] for key in ("num_results", "limit") if key in args]
        if tool == "web_search" else []
    )
    if any(type(value) is not int or value < 1 for value in supplied_result_limits):
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
    try:
        budget.claim(tool, url, call_id=call_id, results=results, units=max(1, len(urls)),
                     retry_key=f"{tool}:{url}")
        return {}
    except (RequestBudgetError, ValueError) as exc:
        return {"action": "block", "message": str(exc)}
