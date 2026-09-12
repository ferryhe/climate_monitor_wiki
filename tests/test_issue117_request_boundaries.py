"""Request-boundary and durable-resume requirements, independent of live services."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def binding(tmp_path, *, fetch=2, search=1, runtime=60, attempt=1):
    return {"run_id": "boundary", "attempt": attempt, "effective_sha256": "a" * 64,
            "checkpoint_dir": str(tmp_path / "checkpoint"),
            "budgets": {"fetch_attempts": fetch, "search_attempts": search,
                        "search_results": 5, "retries_per_item": 2, "runtime_seconds": runtime}}


def budget(tmp_path, **kwargs):
    from climate_monitor.request_budget import RequestBudget
    return RequestBudget(tmp_path / "request-budget.json", binding(tmp_path, **kwargs))


def _tagged_search_result(*, web=None, success=True, error=None):
    result = {"success": success, "data": {"web": web or []}}
    if error is not None:
        result["error"] = error
    return (
        '<untrusted_tool_result source="web_search">\n'
        "External content follows.\n\n"
        + json.dumps(result)
        + "\n</untrusted_tool_result>"
    )


def _opaque_search_binding_fixture(tmp_path):
    """Real Hermes web_search envelope shape with model-owned result aliases."""
    from climate_monitor.management import build_task_binding
    from test_issue94_management_console import _definition

    task_binding = build_task_binding(
        _definition(tmp_path), task_version=1, run_id="opaque-search", attempt=1,
    )
    now = task_binding["created_at"]
    searches = [
        {
            "search_ref": "search-a", "query": "query a", "engine": "web_search",
            "status": "success", "attempted_at": now,
            "result_refs": ["search-a-result-1", "search-a-result-2"],
            "budget": {"max_results": 5, "used_results": 2}, "error": None,
        },
        {
            "search_ref": "search-b", "query": "query b", "engine": "web_search",
            "status": "success", "attempted_at": now,
            "result_refs": ["search-b-result-1"],
            "budget": {"max_results": 5, "used_results": 1}, "error": None,
        },
    ]
    urls = [
        ["https://wmo.int/a-1", "https://wmo.int/a-2"],
        ["https://wmo.int/b-1"],
    ]

    def item(search_index, result_index):
        url = urls[search_index][result_index]
        return {
            "url": url, "title": "", "summary": "", "source": "wmo",
            "discovered_at": now, "discovery_kind": "search",
            "discovery_ref": searches[search_index]["result_refs"][result_index],
            "discovery_search_ref": searches[search_index]["search_ref"],
            "published_date": None, "publication_date_evidence": None,
            "selected": True, "selection_reason": "candidate",
            "processing_status": "pending", "processing_error": None,
            "evidence": {
                "status": "deferred", "fetched_at": now, "final_url": None,
                "attempts": [], "selected_method": None, "content_type": None,
                "content": None, "content_hash": None, "content_ref": None,
                "raw_snapshot_ref": None, "raw_snapshot_sha256": None,
                "classification": "error", "failure_reason": "not fetched yet",
                "http_status": None,
            },
        }

    def event(index):
        result = _tagged_search_result(web=[
                {"url": url, "title": f"Result {ordinal}", "description": "trusted"}
                for ordinal, url in enumerate(urls[index], start=1)
            ])
        return {
            "session_id": "session-real", "tool_call_id": f"call-{index + 1}",
            "status": "ok",
            "tool": "web_search", "arguments": {"query": searches[index]["query"]},
            "result": result,
        }

    payload = {
        "schema_version": "pre-report-acquisition-batch.v1",
        "batch_id": task_binding["acquisition_batch_id"],
        "report_date": task_binding["report_date"],
        "started_at": now, "completed_at": None,
        "date_policy": task_binding["date_policy"],
        "search_decision": {"status": "attempted", "reason": None},
        "searches": searches,
        "items": [item(0, 1), item(1, 0)],
    }
    return task_binding, payload, [event(0), event(1)]


def test_opaque_search_refs_bind_to_same_trusted_event_urls(tmp_path):
    import copy
    import scripts.run_agent_acquisition as runner

    task_binding, payload, events = _opaque_search_binding_fixture(tmp_path)
    assert runner._validate_agent_payload(task_binding, payload, events) == payload

    false_failure = copy.deepcopy(payload)
    false_failure["searches"][0]["status"] = "failed"
    false_failure["searches"][0]["error"] = "model-only failure"
    with pytest.raises(ValueError, match="status.*durable trusted search"):
        runner._validate_agent_payload(task_binding, false_failure, events)

    fabricated_url = copy.deepcopy(payload)
    fabricated_url["items"][0]["url"] = "https://wmo.int/fabricated"
    with pytest.raises(ValueError, match="same trusted search event"):
        runner._validate_agent_payload(task_binding, fabricated_url, events)

    swapped_url = copy.deepcopy(payload)
    swapped_url["items"][0]["url"] = payload["items"][1]["url"]
    with pytest.raises(ValueError, match="same trusted search event"):
        runner._validate_agent_payload(task_binding, swapped_url, events)

    orphan_ref = copy.deepcopy(payload)
    orphan_ref["items"][0]["discovery_ref"] = "orphan-result-ref"
    with pytest.raises(ValueError, match="same trusted search event"):
        runner._validate_agent_payload(task_binding, orphan_ref, events)

    crossed_ref = copy.deepcopy(payload)
    crossed_ref["items"][0]["discovery_ref"] = payload["searches"][1]["result_refs"][0]
    with pytest.raises(ValueError, match="same trusted search event"):
        runner._validate_agent_payload(task_binding, crossed_ref, events)

    count_mismatch = copy.deepcopy(payload)
    count_mismatch["searches"][0]["result_refs"].pop()
    with pytest.raises(ValueError, match="one-to-one.*trusted search event"):
        runner._validate_agent_payload(task_binding, count_mismatch, events)

    ambiguous_events = copy.deepcopy(events)
    duplicate = copy.deepcopy(events[0])
    duplicate["tool_call_id"] = "call-distinct-but-ambiguous"
    ambiguous_events.append(duplicate)
    with pytest.raises(ValueError, match="one-to-one.*trusted search event"):
        runner._validate_agent_payload(task_binding, payload, ambiguous_events)


def test_feedback_event_delta_retains_new_durable_calls_and_legacy_snapshots(tmp_path):
    import copy
    import scripts.run_agent_acquisition as runner

    task_binding, _payload, _events = _opaque_search_binding_fixture(tmp_path)
    primary = [
        {
            "session_id": "session-primary", "tool_call_id": f"call-{index}",
            "tool": "web_search", "arguments": {"query": f"primary {index}"},
            "result": _tagged_search_result(),
        }
        for index in range(8)
    ]
    delta = runner._feedback_tool_event_delta(primary, copy.deepcopy(primary))
    assert delta == []
    empty = _empty_agent_payload(task_binding, reason="Feedback selected no new items")
    assert runner._validate_agent_payload(task_binding, empty, delta) == empty

    new_event = {
        "session_id": "session-feedback", "tool_call_id": "call-new",
        "tool": "web_search", "arguments": {"query": "feedback refinement"},
        "result": _tagged_search_result(),
    }
    cumulative = [*copy.deepcopy(primary), new_event]
    delta = runner._feedback_tool_event_delta(primary, cumulative)
    assert delta == [new_event]

    with pytest.raises(ValueError, match="unreported web_search"):
        runner._validate_agent_payload(task_binding, empty, delta)
    reported = copy.deepcopy(empty)
    reported["search_decision"] = {"status": "attempted", "reason": None}
    reported["searches"] = [{
        "search_ref": "feedback-search", "query": "feedback refinement",
        "engine": "web_search", "status": "success",
        "attempted_at": task_binding["created_at"], "result_refs": [],
        "budget": {"max_results": 5, "used_results": 0}, "error": None,
    }]
    assert runner._validate_agent_payload(task_binding, reported, delta) == reported
    assert len(runner._merge_tool_event_snapshots(primary, cumulative)) == 9

    legacy = {"tool": "web_search", "arguments": {"query": "legacy"}, "result": {}}
    assert runner._feedback_tool_event_delta(primary, [legacy]) == [legacy]


def test_search_item_url_uses_repository_canonical_identity(tmp_path):
    import copy
    from climate_monitor.dedupe import canonical_url
    import scripts.run_agent_acquisition as runner

    task_binding, payload, events = _opaque_search_binding_fixture(tmp_path)
    trusted_url = (
        "https://www.iais.org/2025/04/iais-publishes-comprehensive-application-paper-"
        "on-the-supervision-of-climate-related-risks-in-the-insurance-sector"
    )
    events[0]["result"] = _tagged_search_result(web=[
        {"url": trusted_url}, {"url": "https://wmo.int/a-2"},
    ])
    payload["items"][0]["url"] = trusted_url + "/"
    payload["items"][0]["discovery_ref"] = "search-a-result-1"
    assert canonical_url(payload["items"][0]["url"]) == canonical_url(trusted_url)
    assert runner._validate_agent_payload(task_binding, payload, events) == payload

    same_domain_different_path = copy.deepcopy(payload)
    same_domain_different_path["items"][0]["url"] = "https://www.iais.org/not-this-result"
    with pytest.raises(ValueError, match="same trusted search event"):
        runner._validate_agent_payload(task_binding, same_domain_different_path, events)

    crossed_search = copy.deepcopy(payload)
    crossed_search["items"][0]["url"] = payload["items"][1]["url"]
    with pytest.raises(ValueError, match="same trusted search event"):
        runner._validate_agent_payload(task_binding, crossed_search, events)


def test_fetch_event_explicit_targets_override_result_mentions():
    import scripts.run_agent_acquisition as runner

    first = "https://www.ipcc.ch/2026/08/03/srcities-lam4/"
    second = "https://www.ipcc.ch/2026/08/13/prslcfsod/"
    mention_only = {
        "arguments": {
            "urls": [
                "https://www.ipcc.ch/news/",
                "https://www.iais.org/activities-topics/climate-risk",
            ],
        },
        "result": {"content": f"Related links: {first} and {second}"},
    }
    batched = {
        "arguments": {"urls": [first.rstrip("/"), second]},
        "result": {"content": "Fetched both requested pages"},
    }

    assert not runner._event_supports_url(mention_only, first)
    assert not runner._event_supports_url(mention_only, second)
    assert runner._event_supports_url(batched, first)
    assert runner._event_supports_url(batched, second)
    assert not runner._event_supports_url(
        batched, "https://www.ipcc.ch/2026/08/04/not-the-target/",
    )
    assert runner._event_supports_url(
        {"arguments": {"char_limit": 12000}, "result": {"content": first}}, first,
    )


@pytest.mark.parametrize(
    ("published_date", "evidence_text"),
    [
        ("2025-04-16", "16 Apr 2025"),
        ("2026-09-01", "Published 1 September 2026"),
        ("2026-09-02", "Sep 2, 2026"),
        ("2026-09-02", "September 2, 2026"),
        ("2026-09-02", "Published Sep 2, 2026"),
        ("2026-09-02", "Published September 2, 2026"),
    ],
)
def test_publication_date_accepts_bounded_english_equivalent_in_same_url_event(
    tmp_path, published_date, evidence_text,
):
    import copy
    import scripts.run_agent_acquisition as runner

    task_binding, payload, events = _opaque_search_binding_fixture(tmp_path)
    item = payload["items"][0]
    item["published_date"] = published_date
    item["publication_date_evidence"] = {
        "kind": "publisher", "url": item["url"], "text": evidence_text,
    }
    item["evidence"]["attempts"] = [{"engine": "web_extract", "status": "success"}]
    fetch = {
        "tool": "web_extract", "arguments": {"url": item["url"]},
        "result": {"url": item["url"], "content": f"Publisher page\n{evidence_text}"},
    }
    assert runner._validate_agent_payload(task_binding, payload, [*events, fetch]) == payload

    for wrong_text in (
        "17 Apr 2025", "16 May 2025", "16 Apr 2026", "16 Apr", "04/05/2025",
        "Related article 16 Apr 2025",
    ):
        wrong = copy.deepcopy(payload)
        wrong["items"][0]["published_date"] = "2025-04-16"
        wrong["items"][0]["publication_date_evidence"]["text"] = wrong_text
        wrong_fetch = copy.deepcopy(fetch)
        wrong_fetch["result"]["content"] = f"Publisher page\n{wrong_text}"
        with pytest.raises(ValueError, match="publication-date evidence"):
            runner._validate_agent_payload(task_binding, wrong, [*events, wrong_fetch])

    crossed = copy.deepcopy(fetch)
    crossed["arguments"]["url"] = "https://wmo.int/other-article"
    crossed["result"]["url"] = "https://wmo.int/other-article"
    with pytest.raises(ValueError, match="trusted fetch event|publication-date evidence"):
        runner._validate_agent_payload(task_binding, payload, [*events, crossed])


def test_publication_date_rejects_month_first_near_misses():
    import scripts.run_agent_acquisition as runner

    for text in (
        "September 3, 2026", "October 2, 2026", "September 2, 2025",
        "September 2", "September 2 2026", "09/02/2026",
        "Related article September 2, 2026",
    ):
        assert not runner._publication_date_text_matches("2026-09-02", text)


def test_same_result_url_may_belong_to_two_distinct_search_attempts(tmp_path):
    from climate_registry.acquisition import load_acquisition_batch, store_acquisition_batch
    import scripts.run_agent_acquisition as runner

    task_binding, payload, events = _opaque_search_binding_fixture(tmp_path)
    shared_url = "https://www.ipcc.ch/news/"
    events[0]["result"] = _tagged_search_result(web=[
        {"url": shared_url}, {"url": "https://wmo.int/a-2"},
    ])
    events[1]["result"] = _tagged_search_result(web=[{"url": shared_url}])
    payload["searches"][0]["result_refs"][0] = shared_url
    payload["searches"][1]["result_refs"][0] = shared_url
    payload["items"][0]["url"] = shared_url
    payload["items"][0]["discovery_ref"] = shared_url
    payload["items"][1]["url"] = shared_url
    payload["items"][1]["discovery_ref"] = shared_url

    assert runner._validate_agent_payload(task_binding, payload, events) == payload
    store_acquisition_batch(task_binding["registry_database"], payload)
    loaded = load_acquisition_batch(
        task_binding["registry_database"], task_binding["acquisition_batch_id"],
    )
    assert {
        (origin["search_ref"], origin["discovery_ref"])
        for item in loaded["items"] for origin in item["origins"]
    } == {("search-a", shared_url), ("search-b", shared_url)}


def test_result_alias_must_be_unique_within_its_search_attempt(tmp_path):
    import scripts.run_agent_acquisition as runner

    task_binding, payload, events = _opaque_search_binding_fixture(tmp_path)
    payload["searches"][0]["result_refs"] = ["duplicate-result", "duplicate-result"]
    payload["items"][0]["discovery_ref"] = "duplicate-result"

    with pytest.raises(ValueError, match="result_refs.*unique within.*search"):
        runner._validate_agent_payload(task_binding, payload, events)


def test_search_ref_must_be_unique_before_event_mapping(tmp_path):
    import scripts.run_agent_acquisition as runner

    task_binding, payload, events = _opaque_search_binding_fixture(tmp_path)
    payload["searches"][1]["search_ref"] = payload["searches"][0]["search_ref"]
    payload["items"][1]["discovery_search_ref"] = payload["searches"][0]["search_ref"]

    with pytest.raises(ValueError, match="search_ref.*unique"):
        runner._validate_agent_payload(task_binding, payload, events)


def test_resume_item_identity_scopes_result_ref_to_its_search_ref(tmp_path):
    import scripts.run_agent_acquisition as runner

    task_binding, payload, _events = _opaque_search_binding_fixture(tmp_path)
    shared = payload["items"][0]
    prior = {**shared, "discovery_ref": "shared-result", "discovery_search_ref": "search-a"}
    current = {**shared, "discovery_ref": "shared-result", "discovery_search_ref": "search-b"}
    merged = runner._merge_resume_payload(task_binding, {
        **payload,
        "completed_at": "2099-01-01T00:00:00Z",
        "searches": [{**payload["searches"][1], "search_ref": "search-b"}],
        "items": [current],
    }, {
        "batch_started_at": payload["started_at"],
        "successful_searches": [{**payload["searches"][0], "search_ref": "search-a"}],
        "resolved_items": [prior],
    })

    assert {
        (item["discovery_search_ref"], item["discovery_ref"])
        for item in merged["items"]
    } == {("search-a", "shared-result"), ("search-b", "shared-result")}
    assert merged["completed_at"] is None


def test_terra_response_contract_names_exact_search_result_pair(tmp_path):
    import scripts.run_agent_acquisition as runner

    task_binding, _payload, _events = _opaque_search_binding_fixture(tmp_path)
    prompt = " ".join(runner._prompt(tmp_path / "attempt-1.json", task_binding).split())
    assert (
        "discovery_search_ref must name the exact successful search attempt that returned "
        "the item's URL" in prompt
    )
    assert "discovery_ref must be one of that same attempt's existing result_refs" in prompt


def test_terra_response_contract_requires_every_executed_search(tmp_path):
    import scripts.run_agent_acquisition as runner

    task_binding, _payload, _events = _opaque_search_binding_fixture(tmp_path)
    prompt = " ".join(runner._prompt(tmp_path / "attempt-1.json", task_binding).split())
    assert "Every admitted and executed web_search call must appear exactly once in searches" in prompt
    assert "including auxiliary or refinement queries and searches that produced zero selected items" in prompt
    assert "Never omit an executed search merely because none of its results became an item" in prompt


def test_terra_search_contract_prioritizes_unsearched_source_gaps(tmp_path):
    import scripts.run_agent_acquisition as runner

    task_binding, _payload, _events = _opaque_search_binding_fixture(tmp_path)
    prompt = " ".join(runner._prompt(tmp_path / "attempt-1.json", task_binding).split())
    assert "Each web_search call may request at most 10 results" in prompt
    assert "The global search limit is finite and is not a per-source guarantee" in prompt
    assert (
        "Before refining a source already searched, prioritize a first search for each "
        "bound source that still has a coverage gap and has not yet been searched"
        in prompt
    )
    assert (
        "If a source receives no search opportunity, preserve that source as an explicit "
        "coverage gap" in prompt
    )


def test_terra_response_contract_rejects_invented_day_for_partial_date(tmp_path):
    import scripts.run_agent_acquisition as runner

    task_binding, _payload, _events = _opaque_search_binding_fixture(tmp_path)
    prompt = " ".join(runner._prompt(tmp_path / "attempt-1.json", task_binding).split())
    assert (
        "Set published_date only when trusted evidence gives an explicit complete day, "
        "month, and year" in prompt
    )
    assert (
        "Month-year evidence such as February 2026 or Publication: April 2026, and "
        "year-only evidence, are incomplete" in prompt
    )
    assert (
        "set both published_date and publication_date_evidence to null; never infer or "
        "fill in the first day of a month" in prompt
    )
    assert (
        "publication_date_evidence.text must copy only the standalone complete date "
        "expression" in prompt
    )
    assert "copy 31 Mar 2025, never 31 Mar 2025 in Latest news" in prompt


def test_terra_response_contract_selects_relevant_items_before_body_read(tmp_path):
    import scripts.run_agent_acquisition as runner

    task_binding, _payload, _events = _opaque_search_binding_fixture(tmp_path)
    prompt = " ".join(runner._prompt(tmp_path / "attempt-1.json", task_binding).split())
    assert (
        "selected expresses relevance based on trusted discovery or search evidence"
        in prompt
    )
    assert (
        "A relevant item that needs an article-body read must use selected true and "
        "processing_status pending" in prompt
    )
    assert (
        "initial unavailable or deferred body evidence does not make a relevant item "
        "selected false" in prompt
    )
    assert "The trusted runner subsequently performs the controlled article-body read" in prompt
    assert "Never select an irrelevant item or an item without a trusted URL" in prompt


def test_terra_response_contract_separates_discovery_from_body_fetch_evidence(tmp_path):
    import scripts.run_agent_acquisition as runner

    task_binding, _payload, _events = _opaque_search_binding_fixture(tmp_path)
    prompt = " ".join(runner._prompt(tmp_path / "attempt-1.json", task_binding).split())
    assert "items[].evidence.attempts" in prompt
    assert (
        "contains only actual item-body fetch calls that explicitly targeted that item's URL"
        in prompt
    )
    assert (
        "Record web_extract/browser_exec or their accepted aliases only; never add "
        "web_search, governed_http, or preloaded controlled-site evidence" in prompt
    )
    assert "Evidence content is exact body text returned by that matched tool event" in prompt
    assert "never a summary or paraphrase" in prompt
    assert "were all supplied by trusted run evidence; never calculate, guess, or invent them" in prompt
    assert (
        "Otherwise use unavailable, deferred, or failed status with classification error, "
        "actual failure_reason, and null selected_method, content, content_hash, content_ref, "
        "raw_snapshot_ref, and raw_snapshot_sha256" in prompt
    )


def test_failed_durable_search_cannot_be_reported_as_zero_result_success(tmp_path):
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    from climate_monitor.request_budget import RequestBudget, ledger_path
    from test_issue94_management_console import _write_hermes_tool_events
    import scripts.run_agent_acquisition as runner

    task_binding, payload, events = _opaque_search_binding_fixture(tmp_path)
    payload["searches"][0]["result_refs"] = []
    payload["searches"][0]["budget"]["used_results"] = 0
    payload["items"] = [payload["items"][1]]
    events[0]["status"] = "error"
    events[0]["result"] = _tagged_search_result(
        success=False, error="upstream timeout",
    )
    _write_hermes_tool_events(attempt_home(task_binding), task_binding, events)
    ledger = RequestBudget(ledger_path(task_binding), task_binding)
    for event in events:
        call_id = (
            f"{task_binding['attempt']}:session-{task_binding['attempt']}:"
            f"{event['tool_call_id']}"
        )
        ledger.claim(
            "web_search", event["arguments"]["query"], call_id=call_id, results=5,
        )
        ledger.complete_tool(call_id, event["result"], event["status"])
    trusted_events = runner._trusted_tool_events(task_binding)
    assert [event["durable_status"] for event in trusted_events] == ["error", "ok"]

    with pytest.raises(ValueError, match="status.*durable trusted search"):
        runner._validate_agent_payload(task_binding, payload, trusted_events)


def test_json_looking_description_cannot_create_a_trusted_result_url(tmp_path):
    import scripts.run_agent_acquisition as runner

    task_binding, payload, events = _opaque_search_binding_fixture(tmp_path)
    embedded_url = "https://wmo.int/not-a-result"
    events[0]["result"] = _tagged_search_result(web=[
        {"url": "https://wmo.int/a-1", "description": json.dumps({"url": embedded_url})},
        {"url": "https://wmo.int/a-2", "description": "trusted"},
    ])
    assert runner._event_result_urls(events[0]["result"]) == {
        "https://wmo.int/a-1", "https://wmo.int/a-2",
    }
    payload["searches"][0]["result_refs"].append("search-a-result-3")
    payload["searches"][0]["budget"]["used_results"] = 3
    payload["items"][0]["url"] = embedded_url
    payload["items"][0]["discovery_ref"] = "search-a-result-3"

    with pytest.raises(ValueError, match="trusted search event"):
        runner._validate_agent_payload(task_binding, payload, events)


@pytest.mark.parametrize(
    ("trusted_limits", "reported_max"),
    [
        ({"num_results": 2, "limit": 4}, 4),
        ({"limit": 3}, 5),
        ({}, 4),
    ],
)
def test_reported_search_max_must_match_trusted_effective_limit(
    tmp_path, trusted_limits, reported_max,
):
    import scripts.run_agent_acquisition as runner

    task_binding, payload, events = _opaque_search_binding_fixture(tmp_path)
    events[0]["arguments"].update(trusted_limits)
    payload["searches"][0]["budget"]["max_results"] = reported_max

    with pytest.raises(ValueError, match="max_results.*trusted search"):
        runner._validate_agent_payload(task_binding, payload, events)

    payload["searches"][0]["budget"]["max_results"] = trusted_limits.get(
        "num_results", trusted_limits.get("limit", 5),
    )
    assert runner._validate_agent_payload(task_binding, payload, events) == payload


def test_default_covers_all_seed_and_bounded_article_work():
    from climate_monitor.management import default_task_definition
    from climate_monitor.request_budget import DEFAULT_SEARCH_RESULTS_PER_CALL

    value = default_task_definition()["parameters"]["budgets"]
    assert value == {
        "search_attempts": 36,
        "search_results": 360,
        "fetch_attempts": 5000,
        "retries_per_item": 2,
        "runtime_seconds": 3600,
    }
    assert DEFAULT_SEARCH_RESULTS_PER_CALL == 10
    assert value["search_results"] == value["search_attempts"] * DEFAULT_SEARCH_RESULTS_PER_CALL
    # Four sends per seed/article operation, one ten-result search per source,
    # two retries per article, plus 120 native fetch-tool units.
    required_fetch_units = 116 * 4 + 360 * 3 * 4 + 120
    assert required_fetch_units == 4904
    assert value["fetch_attempts"] >= required_fetch_units
    management_javascript = (
        Path(__file__).resolve().parents[1] / "management_ui" / "manage.js"
    ).read_text(encoding="utf-8")
    assert "Object.entries(parameters.budgets)" in management_javascript
    assert "form.get('budget_' + key)" in management_javascript


def test_container_packages_default_report_run_config():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    dockerignore_entries = {
        line.strip()
        for line in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "!monitoring/run_config.yaml" in dockerignore_entries
    assert "COPY monitoring/run_config.yaml ./monitoring/run_config.yaml" in dockerfile


def test_container_requires_and_embeds_exact_repository_commit():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")

    assert "ARG CLIMATE_REPOSITORY_COMMIT_SHA" in dockerfile
    assert "ENV CLIMATE_REPOSITORY_COMMIT_SHA=$CLIMATE_REPOSITORY_COMMIT_SHA" in dockerfile
    assert "repository commit SHA must be a 40-character lowercase hex digest" in dockerfile
    assert "CLIMATE_REPOSITORY_COMMIT_SHA: ${CLIMATE_REPOSITORY_COMMIT_SHA:-}" in compose
    assert '--build-arg CLIMATE_REPOSITORY_COMMIT_SHA="${{ github.sha }}"' in workflow
    assert ".git/" in dockerignore
    assert "COPY .git" not in dockerfile


def test_managed_repository_commit_requires_exact_runtime_value_or_real_checkout(
    monkeypatch,
):
    import subprocess
    from climate_monitor import management

    exact = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=Path(__file__).resolve().parents[1], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    monkeypatch.setenv("CLIMATE_REPOSITORY_COMMIT_SHA", exact)
    monkeypatch.setattr(
        management.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("configured container path must not call git"),
    )
    assert management.resolve_repository_commit_sha() == exact

    monkeypatch.setenv("CLIMATE_REPOSITORY_COMMIT_SHA", "not-a-revision")
    with pytest.raises(ValueError, match="CLIMATE_REPOSITORY_COMMIT_SHA.*40-character"):
        management.resolve_repository_commit_sha()

    monkeypatch.delenv("CLIMATE_REPOSITORY_COMMIT_SHA")
    monkeypatch.setattr(
        management.subprocess, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("git")),
    )
    with pytest.raises(ValueError, match="CLIMATE_REPOSITORY_COMMIT_SHA.*real Git checkout"):
        management.resolve_repository_commit_sha()


def test_report_process_receives_frozen_repository_commit(tmp_path, monkeypatch):
    import subprocess
    import scripts.run_agent_acquisition as runner

    exact = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=Path(__file__).resolve().parents[1], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    task_binding = {
        "provider": "openai-api", "model": "test-model",
        "repository_commit_sha": exact,
        "report_inputs": {
            key: str(tmp_path / key)
            for key in (
                "acquisition_batch", "web_listening_manifest", "pillar_b_artifact",
                "staging_dir", "state_dir", "source_dir", "wiki_dir",
            )
        },
    }
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setattr(runner, "_report_environment", lambda _provider: {})
    assert runner._run_report(tmp_path / "attempt-1.json", task_binding) == 0
    command = observed["command"]
    assert command[command.index("--repository-commit-sha") + 1] == exact


def test_managed_finalize_passes_bound_commit_without_git_lookup(tmp_path, monkeypatch):
    import hashlib
    import subprocess
    from scripts import run_climate_monitor as monitor
    from climate_monitor.weekly_monitor.prompt_loader import LoadedPrompt

    exact = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=monitor.ROOT,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    staging = tmp_path / "staging"
    staging.mkdir()
    response = staging / "response.json"
    public = tmp_path / "public.json"
    bound_path = tmp_path / "attempt-1.json"
    for path, value in (
        (response, {}), (public, {}), (bound_path, {}),
        (staging / "v2_authoring_request.json", {}),
        (staging / "stats.json", {}),
        (staging / "article_evidence.json", {"records": []}),
    ):
        path.write_text(json.dumps(value), encoding="utf-8")
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    task_binding = {
        "provider": "openai-api", "model": "test-model",
        "repository_commit_sha": exact,
    }
    frozen_raw = b"frozen article-summary prompt"
    frozen_prompt = LoadedPrompt(
        prompt_id="article_summary", version="v1", path=bound_path,
        raw_bytes=frozen_raw, sha256=hashlib.sha256(frozen_raw).hexdigest(),
    )
    bundle = {
        "report_date": "2026-09-07", "stats": {},
        "public_artifacts": {
            name: {"path": str(public), "sha256": digest(public)}
            for name in ("acquisition_batch", "web_listening_manifest", "pillar_b_artifact")
        },
        "registry_acquisition": {
            "task_binding": {"path": str(bound_path), "sha256": digest(bound_path)},
        },
        "execution_binding": {**task_binding},
        "taxonomy": {"sha256": "taxonomy"},
        "prompt": {"sha256": frozen_prompt.sha256},
    }
    monkeypatch.setattr(monitor, "_read_staging_bundle", lambda _path: bundle)
    monkeypatch.setattr(monitor, "_verify_staging_digest", lambda *_args: None)
    monkeypatch.setattr(monitor, "_validate_v2_stats_shape", lambda _stats: {})
    monkeypatch.setattr(monitor, "load_authoring_response", lambda _path: {})
    taxonomy = SimpleNamespace(sha256="taxonomy")
    monkeypatch.setattr(
        monitor, "_load_task_binding_with_taxonomy",
        lambda _path: (task_binding, bound_path, taxonomy),
    )
    monkeypatch.setattr(monitor, "_bound_prompt", lambda *_args: frozen_prompt)
    for name in ("_verify_candidate_selection", "validate_authoring_response"):
        monkeypatch.setattr(monitor, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(monitor, "_candidate_items_from_evidence", lambda *_args: [])
    monkeypatch.setattr(monitor, "_read_prepare_inputs", lambda *_args, **_kwargs: ({}, {}, {}, [], {}))
    monkeypatch.setattr(monitor, "_outcome_to_article_changes", lambda *_args: {})
    observed = {}
    monkeypatch.setattr(
        monitor, "run_weekly_monitor",
        lambda **kwargs: observed.update(kwargs) or "completed",
    )
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    args = SimpleNamespace(
        authoring_response=str(response), staging_dir=str(staging),
        repository_commit_sha=exact, model="test-model", model_provider="openai-api",
        source_config="monitoring/supranational_sources.yaml",
        run_config="monitoring/run_config.yaml", site_scopes="",
        state_dir=str(tmp_path / "state"), source_dir=str(source_dir),
        wiki_dir=str(tmp_path / "wiki"), no_sync=True, no_update_seen_state=True,
        article_evidence_loopback="",
    )

    assert monitor._run_finalize(args, SimpleNamespace(error=pytest.fail)) == "completed"
    assert observed["repository_commit_sha"] == exact
    assert observed["loaded_prompt"] is frozen_prompt


def test_target_redirect_and_failure_reservations_are_durable(tmp_path):
    from climate_monitor.request_budget import GuardedGateway, RequestBudgetError
    ledger = budget(tmp_path, fetch=1)
    sends = []
    class Gateway:
        user_agent = "test"
        def read(self, url, *, before_target_request, **kwargs):
            for target in (url, url + "redirect"):
                release = before_target_request(target, SimpleNamespace(decision_id="decision"))
                sends.append(target)
                if release:
                    release()
    with pytest.raises(RequestBudgetError):
        GuardedGateway(Gateway(), ledger, "seed", transport_timeout_seconds=30).read("https://example.test/")
    assert sends == ["https://example.test/"]
    assert budget(tmp_path, fetch=1).usage()["fetch_attempts"] == 1
    assert any(event["event_kind"] == "precheck" for event in ledger.events())


def test_policy_rejection_has_no_target_charge(tmp_path):
    from climate_monitor.request_budget import GuardedGateway
    ledger = budget(tmp_path)
    class Gateway:
        def read(self, url, **kwargs):
            raise RuntimeError("policy rejection before callback")
    with pytest.raises(RuntimeError, match="policy"):
        GuardedGateway(Gateway(), ledger, "seed", transport_timeout_seconds=30).read("https://example.test/")
    assert ledger.usage()["fetch_attempts"] == 0


def test_guarded_gateway_caps_implicit_and_explicit_timeouts_before_send():
    from climate_monitor.request_budget import GuardedGateway

    observed = []
    claims = []
    remaining = iter((3600.0, 3600.0, 3600.0, 3600.0, 4.0))

    class Budget:
        def remaining_seconds(self):
            return next(remaining)

        def claim(self, kind, target, **kwargs):
            claims.append((kind, target))

    class Gateway:
        def read(self, url, *, before_target_request, timeout_seconds):
            observed.append(timeout_seconds)
            before_target_request(url, None)

    gateway = GuardedGateway(
        Gateway(), Budget(), "seed", transport_timeout_seconds=30.0,
    )
    gateway.read("https://example.test/default")
    gateway.read("https://example.test/none", timeout_seconds=None)
    gateway.read("https://example.test/shorter", timeout_seconds=5.0)
    gateway.read("https://example.test/longer", timeout_seconds=120.0)
    gateway.read("https://example.test/remaining")

    assert observed == [30.0, 30.0, 5.0, 30.0, 4.0]
    assert claims == [
        ("http", "https://example.test/default"),
        ("http", "https://example.test/none"),
        ("http", "https://example.test/shorter"),
        ("http", "https://example.test/longer"),
        ("http", "https://example.test/remaining"),
    ]


def test_deadline_blocks_before_send(tmp_path, monkeypatch):
    import climate_monitor.request_budget as module
    monkeypatch.setattr(module.time, "time", lambda: 100.0)
    ledger = budget(tmp_path, runtime=10)
    monkeypatch.setattr(module.time, "time", lambda: 110.0)
    with pytest.raises(module.RequestBudgetError, match="runtime"):
        ledger.claim("http", "https://example.test/")
    assert ledger.usage()["fetch_attempts"] == 0


def test_ledger_cannot_reset_or_change_limits_on_resume(tmp_path):
    from climate_monitor.request_budget import RequestBudget, RequestBudgetError
    ledger = budget(tmp_path, fetch=1)
    ledger.claim("http", "https://example.test/")
    ledger.finish()
    resumed = budget(tmp_path, fetch=1, attempt=2)
    with pytest.raises(RequestBudgetError):
        resumed.claim("http", "https://example.test/next")
    with pytest.raises(ValueError, match="identity|limits"):
        budget(tmp_path, fetch=2, attempt=2)
    with pytest.raises(ValueError, match="missing"):
        RequestBudget(tmp_path / "missing.json", binding(tmp_path, attempt=2))


@pytest.mark.parametrize("tool", ["web_search", "web_extract", "browser_exec"])
def test_hermes_pre_hook_blocks_handler_at_exact_boundary(tmp_path, tool):
    from climate_monitor.request_budget import hook_decision
    ledger = budget(tmp_path, fetch=1, search=1)
    args = {"query": "climate", "num_results": 5} if tool == "web_search" else {"url": "https://example.test/"}
    payload = {"hook_event_name": "pre_tool_call", "tool_name": tool,
               "tool_input": args, "session_id": "s", "extra": {"tool_call_id": "1"}}
    invoked = []
    for call_id in ("1", "2"):
        payload["extra"]["tool_call_id"] = call_id
        directive = hook_decision(ledger, payload)
        if directive.get("action") != "block":
            invoked.append(call_id)
    assert invoked == ["1"]
    assert len([e for e in ledger.events() if e["event_kind"] == "tool"]) == 1


def test_unsupported_article_never_invokes_unguarded_reader(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter as article
    called = []
    monkeypatch.setattr(article, "_import_public_reader", lambda: SimpleNamespace(
        fetch_article_content=lambda url: called.append(url)))
    record = article.fetch_article_content("a", "https://example.test/", budget=budget(tmp_path))
    assert called == []
    assert record["status"] == "unavailable"
    assert "before_target_request" in record["failure_reason"]
    assert record["attempts"][0]["event_kind"] == "unsupported"


def test_receipt_is_hash_verified_and_shared_across_attempts(tmp_path):
    ledger = budget(tmp_path)
    ledger.save_receipt("seed", {"checkpoint": {"content_hash": "a", "links": []}})
    ledger.finish()
    resumed = budget(tmp_path, attempt=2)
    assert resumed.receipt("seed")["checkpoint"]["content_hash"] == "a"
    value = json.loads((tmp_path / "request-budget.json").read_text())
    value["receipts"]["seed"]["payload"]["checkpoint"]["content_hash"] = "tampered"
    (tmp_path / "request-budget.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="hash|digest"):
        resumed.receipt("seed")


def test_pinned_shell_hook_runs_before_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    hooks = pytest.importorskip("agent.shell_hooks")
    import shlex
    import sys
    from climate_monitor.request_budget import ledger_path
    from climate_monitor.hermes_acquisition_hooks import install_hooks
    b = binding(tmp_path, fetch=1)
    path = tmp_path / "attempt-1.json"
    path.write_text(json.dumps(b))
    ledger = budget(tmp_path, fetch=1)
    # Use the actual installed Python runtime, with a CLI-shaped entrypoint.
    executable = tmp_path / "hermes"
    executable.write_text(f"#!{sys.executable}\n")
    executable.chmod(0o700)
    env, home = install_hooks([str(executable)], path, b, {"PATH": __import__('os').environ['PATH']})
    config = json.loads((home / "config.yaml").read_text())
    spec = next(s for s in hooks.iter_configured_hooks(config) if s.event == "pre_tool_call")
    invoked = []
    for call_id in ("1", "2"):
        outcome = hooks.run_once(spec, {"tool_name": "web_extract", "args": {"url": "https://example.test/"},
                                       "session_id": "test", "tool_call_id": call_id})
        if (outcome.get("parsed") or {}).get("action") != "block":
            invoked.append(call_id)
    assert invoked == ["1"]
    assert env["HERMES_HOME"] == str(home)
    assert spec.fail_closed


def test_failed_source_projection_keeps_artifact_and_no_full_success(tmp_path):
    import hashlib
    import scripts.run_agent_acquisition as runner
    manifest = {"manifest_id": "failed-evidence", "source": {"source_id": "example"}}
    path = tmp_path / "failed.json"
    path.write_text(json.dumps(manifest))
    outcome = {"full_success": False, "counts": {"valid_snapshots": 0},
               "dispositions": [{"disposition": "blocked", "artifact_id": None}]}
    row = {"source": "example", "status": "failed", "artifact_id": "failed-evidence",
           "artifact_path": str(path), "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
           "manifest": manifest, "outcome": outcome}
    b = {"source_inventory": {"records": [{"key": "example"}]},
         "report_inputs": {key: str(tmp_path / key) for key in ("acquisition_batch", "web_listening_manifest", "pillar_b_artifact")}}
    payload = {"items": [], "report_date": "2026-09-07", "date_policy": {},
               "search_decision": "no_search", "searches": []}
    runner._write_report_inputs(b, payload, {"status": "completed", "source_results": [row]})
    assert json.loads(Path(b["report_inputs"]["acquisition_batch"]).read_text())[0]["full_success"] is False
    manifest_path = Path(b["report_inputs"]["web_listening_manifest"])
    assert json.loads(manifest_path.read_text()) == []
    assert json.loads(manifest_path.with_suffix(".diagnostics.json").read_text()) == [manifest]
    assert json.loads(path.read_text()) == manifest


def seed_runtime(monkeypatch, sends, interrupt_at=None, outcomes=None):
    from climate_monitor import web_listening_adapter as adapter
    class Gateway:
        user_agent = "web-listening-bot/1.0"
        def close(self): pass
        def read(self, url, *, before_target_request, **kwargs):
            kind = outcomes(url) if outcomes else "success"
            if kind == "rejected":
                error = RuntimeError("governed policy refusal")
                error.envelope = SimpleNamespace(model_dump=lambda **kwargs: {"reason_code": "policy.refused"})
                raise error
            before_target_request(url, SimpleNamespace(decision_id="authorized"))
            sends.append(url)
            if isinstance(kind, BaseException):
                raise kind
            if kind == "incomplete":
                raise OSError("network temporarily unavailable")
            return SimpleNamespace(final_url=url, status_code=200, fit_markdown="Climate risk evidence",
                                   markdown="", content_text="", raw_html="", metadata_json={"links": []})
    class Crawler:
        def __init__(self, *, fetch_mode, read_gateway): self.gateway = read_gateway
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def fetch_page(self, url, **kwargs):
            if interrupt_at is not None and len(sends) == interrupt_at:
                raise KeyboardInterrupt("simulated worker crash after completed receipts")
            return self.gateway.read(url)
    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(adapter, "_load_gateway_builder", lambda: lambda **kwargs: Gateway())
    monkeypatch.setattr(adapter, "_load_web_listening", lambda: (Crawler, {
        "compute_hash": lambda text: "a" * 64, "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
        "find_new_links": lambda old, new: [], "find_document_links": lambda links: [],
    }))


def test_116_seed_crash_resume_has_no_duplicate_completed_sends(tmp_path, monkeypatch):
    from climate_monitor import web_listening_adapter as adapter
    from climate_monitor.config import load_sources, load_site_scopes
    sources = load_sources("monitoring/supranational_sources.yaml")
    scopes = load_site_scopes("monitoring/site_scopes.yaml")
    sends = []
    first = budget(tmp_path, fetch=1200)
    seed_runtime(monkeypatch, sends, interrupt_at=40)
    with pytest.raises(KeyboardInterrupt):
        adapter.collect_website_items_with_evidence(sources, state_dir=tmp_path / "seeds",
                                                   site_scopes=scopes, budget=first)
    assert len(sends) == 40
    # No graceful finish: attempt 2 reconciles the interrupted attempt's time.
    resumed = budget(tmp_path, fetch=1200, attempt=2)
    seed_runtime(monkeypatch, sends)
    _, warnings, evidence = adapter.collect_website_items_with_evidence(sources,
        state_dir=tmp_path / "seeds", site_scopes=scopes, budget=resumed)
    assert len(sends) == 116
    from collections import Counter
    scopes_by_key = {scope.source_key: scope for scope in scopes}
    expected = [url for source in sources for url in adapter._seed_urls(source, scopes_by_key.get(source.key))]
    # There are 116 selected source/seed slots but 114 distinct URLs: two URLs
    # belong to multiple institutions. Preserve those slots, without retries.
    assert Counter(sends) == Counter(expected)
    assert resumed.usage()["fetch_attempts"] == 116
    assert len(evidence["source_results"]) == 36
    assert evidence["full_success"] and not warnings


def test_completed_with_gaps_status_never_says_report_completed(tmp_path):
    from test_issue94_management_console import _store, _definition
    from climate_monitor.management import ManagementService
    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor="operator")
    service = ManagementService(store=store, runtime_root=tmp_path / "runs", launcher=lambda b: 4321)
    started = service.start(trigger="manual")
    root = service._run_dir(started["run_id"])
    (root / "attempt-1-result.json").write_text(json.dumps({"exit_code": 0, "retryable": False,
        "execution_complete": True, "full_coverage": False, "error": "unsupported article guard"}))
    value = service.progress(started["run_id"])
    assert value["stage"] == "completed_with_gaps"
    assert value["report_phase"] == "not_started"
    assert value["coverage"]["full_success"] is False


def test_mixed_run_round_trips_registry_status_and_blocks_report(tmp_path, monkeypatch):
    from test_issue94_management_console import _store, _definition
    from climate_monitor.management import ManagementService
    from climate_registry.acquisition import load_acquisition_batch, readback_source_outcomes, freeze_acquisition_for_report, AcquisitionIncompleteError
    import scripts.run_agent_acquisition as runner
    store = _store(tmp_path)
    definition = _definition(tmp_path)
    definition["parameters"]["source_keys"] = ["iais", "wmo", "ipcc"]
    store.save(definition, actor="operator")
    service = ManagementService(store=store, runtime_root=tmp_path / "runs", launcher=lambda b: 4321)
    started = service.start(trigger="manual")
    b = service.binding(started["run_id"])
    root = service._run_dir(started["run_id"])
    path = root / "attempt-1.json"
    sends = []
    # This binding owns a test-only checkpoint root; no shared repository state.
    b['report_inputs']['state_dir'] = str(tmp_path / 'source-state')
    path.write_text(json.dumps(b))
    seed_runtime(monkeypatch, sends, outcomes=lambda url:
                 "rejected" if "wmo.int" in url else "incomplete" if "ipcc.ch" in url else "success")
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/true")
    monkeypatch.setattr(runner, "_trusted_tool_events", lambda *args, **kwargs: [])
    def invoke(command, response_path, binding_path, binding, deadline):
        payload = {"schema_version": "pre-report-acquisition-batch.v1", "batch_id": b["acquisition_batch_id"],
                   "report_date": b["report_date"], "date_policy": b["date_policy"],
                   "started_at": b["created_at"], "completed_at": b["created_at"],
                   "search_decision": {"status": "no_search", "reason": "Fixture requests source diagnostics only; no supplemental query was executed"},
                   "searches": [], "items": []}
        response_path.write_text(json.dumps({"acquisition_batch": payload}))
        return 0
    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    monkeypatch.setattr(runner, "_run_report", lambda *args: pytest.fail("gaps must not dispatch report"))
    assert runner._execute_locked(path) == 0
    payload = json.loads((root / "attempt-1-acquisition.json").read_text())
    stored = load_acquisition_batch(b["registry_database"], b["acquisition_batch_id"])
    assert stored["completed_at"] is None
    rows = readback_source_outcomes(b["registry_database"], payload)
    assert {row["coverage_status"] for row in rows} == {"success", "rejected", "incomplete"}
    with pytest.raises(AcquisitionIncompleteError):
        freeze_acquisition_for_report(b["registry_database"], b["acquisition_batch_id"], report_date=b["report_date"])
    status = service.progress(started["run_id"])
    assert status["stage"] == "completed_with_gaps"
    assert status["coverage"]["execution_complete"] is True
    assert status["coverage"]["full_success"] is False
    assert status["coverage"]["rejected_sources"] == 1
    assert status["coverage"]["incomplete_sources"] == 1
    assert status["budget"]["used"]["fetch_attempts"] == len(sends)
    assert status["budget"]["used"]["search_attempts"] == 0
    assert not Path(b["frozen_report_input"]).exists()


def test_runner_completes_zero_item_full_coverage_and_round_trips_sources(
    tmp_path, monkeypatch,
):
    import hashlib
    from climate_registry.acquisition import load_acquisition_batch, readback_source_outcomes
    from test_issue94_management_console import _controlled_site_result
    import scripts.run_agent_acquisition as runner

    _service, b, path = _managed_attempt(
        tmp_path, monkeypatch, source_keys=["iais", "ipcc"],
    )
    source_results = [
        _controlled_site_result(tmp_path, source, candidates=[], disposition="unchanged")
        for source in b["source_inventory"]["records"]
    ]
    for row in source_results:
        artifact = tmp_path / f"{row['source']}-manifest.json"
        raw = (json.dumps(row["manifest"], sort_keys=True, indent=2) + "\n").encode()
        artifact.write_bytes(raw)
        row["artifact_path"] = str(artifact)
        row["artifact_sha256"] = hashlib.sha256(raw).hexdigest()
    site_context = {
        "status": "completed", "source_results": source_results,
        "attempts": [], "candidates": [], "warnings": [], "systemic_error": None,
    }
    now = b["created_at"]
    searches = []
    events = []
    for search_index in range(4):
        query = f"trusted zero-item query {search_index}"
        urls = [
            f"https://example.test/search-{search_index}/result-{result_index}"
            for result_index in range(5)
        ]
        searches.append({
            "search_ref": f"search-{search_index}", "query": query,
            "engine": "web_search", "status": "success", "attempted_at": now,
            "result_refs": urls, "budget": {"max_results": 5, "used_results": 5},
            "error": None,
        })
        events.append({
            "session_id": "trusted-session", "tool_call_id": f"call-{search_index}",
            "durable_status": "ok", "tool": "web_search",
            "arguments": {"query": query, "num_results": 5},
            "result": {"data": {"web": [{"url": url} for url in urls]}},
        })
    payload = {
        "schema_version": "pre-report-acquisition-batch.v1",
        "batch_id": b["acquisition_batch_id"], "report_date": b["report_date"],
        "date_policy": b["date_policy"], "started_at": now, "completed_at": None,
        "search_decision": {"status": "attempted", "reason": None},
        "searches": searches, "items": [],
    }
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/true")
    monkeypatch.setattr(runner, "_controlled_site_context", lambda _binding: site_context)
    monkeypatch.setattr(runner, "_trusted_tool_events", lambda *_args, **_kwargs: events)

    def invoke(_command, response_path, *_args):
        response_path.write_text(json.dumps({"acquisition_batch": payload}))
        return 0

    report_calls = []
    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    monkeypatch.setattr(runner, "_run_report", lambda *_args: report_calls.append(True) or 0)
    monkeypatch.setattr(runner, "_commit_controlled_site_checkpoints", lambda _binding: 0)

    assert runner._execute_locked(path) == 0
    persisted_payload = json.loads(path.with_name("attempt-1-acquisition.json").read_text())
    stored = load_acquisition_batch(b["registry_database"], b["acquisition_batch_id"])
    assert persisted_payload["completed_at"] is not None
    assert stored["completed_at"] == persisted_payload["completed_at"]
    assert stored["items"] == []
    assert len(stored["searches"]) == 4
    assert readback_source_outcomes(
        b["registry_database"], persisted_payload,
    ) == persisted_payload["source_outcomes"]
    assert len(persisted_payload["source_outcomes"]) == 2
    assert Path(b["frozen_report_input"]).is_file()
    assert report_calls == [True]


def test_supported_article_redirect_is_guarded_at_public_provider_seam(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter as article
    sends = []
    def reader(url, *, before_target_request, timeout_seconds, **kwargs):
        for target in (url, url + 'redirect'):
            before_target_request(target, None)
            sends.append(target)
        pytest.fail('redirect over the boundary must not complete')
    monkeypatch.setattr(article, '_import_public_reader', lambda: SimpleNamespace(
        fetch_article_content=reader, runtime_data_dir=lambda: tmp_path))
    monkeypatch.setattr(article, '_load_site_scopes', lambda: {
        'example': SimpleNamespace(seed_urls=['https://example.test/'])})
    monkeypatch.setattr(article, '_prepare_public_configuration', lambda *args: (
        SimpleNamespace(site_key='example', model_dump=lambda **kwargs: {}), tmp_path / 'scope.yaml'))
    ledger = budget(tmp_path, fetch=1)
    record = article.fetch_article_content('a', 'https://example.test/', budget=ledger)
    assert sends == ['https://example.test/']
    assert record['status'] != 'ok'
    assert record['attempts'][0]['event_kind'] == 'precheck'
    assert ledger.usage()['fetch_attempts'] == 1


def test_retries_remain_spent_after_failure_and_resume(tmp_path):
    from climate_monitor.request_budget import GuardedGateway, RequestBudgetError
    sends = []
    class Gateway:
        def read(self, url, *, before_target_request, **kwargs):
            before_target_request(url, None)
            sends.append(url)
            raise OSError('transport failed')
    ledger = budget(tmp_path, fetch=20)
    for _ in range(2):
        with pytest.raises(OSError):
            GuardedGateway(Gateway(), ledger, 'seed', transport_timeout_seconds=30).read('https://example.test/')
    ledger.finish()
    resumed = budget(tmp_path, fetch=20, attempt=2)
    with pytest.raises(OSError):
        GuardedGateway(Gateway(), resumed, 'seed', transport_timeout_seconds=30).read('https://example.test/')
    with pytest.raises(RequestBudgetError, match='retry'):
        GuardedGateway(Gateway(), resumed, 'seed', transport_timeout_seconds=30).read('https://example.test/')
    assert len(sends) == 3
    assert resumed.usage()['retries'] == 2
    assert resumed.usage(attempt=2)['retries'] == 1


def test_concurrent_claims_cannot_overdraw(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from climate_monitor.request_budget import RequestBudgetError
    ledger = budget(tmp_path, fetch=2)
    def claim(i):
        try:
            ledger.claim('http', f'https://example.test/{i}')
            return True
        except RequestBudgetError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, range(12))) == 2
    assert ledger.usage()['fetch_attempts'] == 2


def test_failed_search_releases_results_but_spends_attempt(tmp_path):
    from climate_monitor.request_budget import hook_decision
    ledger = budget(tmp_path, search=2)
    payload = {'hook_event_name': 'pre_tool_call', 'tool_name': 'web_search',
               'tool_input': {'query': 'climate', 'num_results': 5},
               'session_id': 's', 'extra': {'tool_call_id': '1'}}
    assert hook_decision(ledger, payload) == {}
    payload.update(hook_event_name='post_tool_call', extra={'tool_call_id': '1', 'status': 'error', 'result': 'failed'})
    assert hook_decision(ledger, payload) == {}
    assert ledger.usage()['search_attempts'] == 1
    assert ledger.usage()['search_results'] == 0
    assert ledger.usage()['search_results_reserved'] == 0
    payload.update(hook_event_name='pre_tool_call', extra={'tool_call_id': '2'})
    assert hook_decision(ledger, payload) == {}


def test_deleted_ledger_cannot_restart_same_attempt(tmp_path):
    ledger = budget(tmp_path)
    ledger.claim('http', 'https://example.test/')
    ledger.path.unlink()
    with pytest.raises(ValueError, match='missing'):
        budget(tmp_path)


def test_replayed_search_completion_cannot_release_spent_results(tmp_path):
    ledger = budget(tmp_path)
    ledger.claim('web_search', 'climate', call_id='call', results=5)
    ledger.complete_tool('call', {'results': [{'url': 'https://example.test/'}]}, 'ok')
    before = ledger.usage()['search_results']
    assert before == 1
    with pytest.raises(ValueError, match='completion'):
        ledger.complete_tool('call', None, 'error')
    assert ledger.usage()['search_results'] == before


def test_incompatible_gateway_hook_fails_once_before_bulk(tmp_path, monkeypatch):
    from climate_monitor import web_listening_adapter as adapter
    from test_issue117_governed_acquisition import install_runtime, source
    calls = install_runtime(monkeypatch)
    class Crawler:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def fetch_page(self, url, **kwargs):
            calls['fetch'].append(url)
            raise RuntimeError('reader cannot guard targets')
    monkeypatch.setattr(adapter, '_load_web_listening', lambda: (Crawler, {}))
    # The old public shape can read but cannot expose target sends.
    monkeypatch.setattr(adapter, '_load_gateway_builder', lambda: lambda **kwargs: SimpleNamespace(
        read=lambda url: None, close=lambda: None, user_agent='web-listening-bot/1.0'))
    with pytest.raises(RuntimeError, match='preflight.*before_target_request'):
        adapter.collect_website_items_with_evidence([source(), source('second')], state_dir=tmp_path)
    assert calls['fetch'] == []


def test_article_dependency_gap_round_trips_registry_and_status(tmp_path, monkeypatch):
    from test_issue94_management_console import _store, _definition
    from test_issue112_acquisition import _item, _batch
    from climate_monitor import article_content_adapter as article
    from climate_monitor.management import ManagementService
    from climate_registry.acquisition import load_acquisition_batch
    import scripts.run_agent_acquisition as runner
    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor='operator')
    service = ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: 4321)
    started = service.start(trigger='manual')
    b = service.binding(started['run_id'])
    root = service._run_dir(started['run_id'])
    path = root / 'attempt-1.json'
    monkeypatch.setattr(article, '_import_public_reader', lambda: SimpleNamespace(
        fetch_article_content=lambda url: pytest.fail('unguarded article provider invoked')))
    payload = _batch([_item(discovery_kind='site', discovery_ref='site:wmo', discovery_search_ref=None)],
        batch_id=b['acquisition_batch_id'], report_date=b['report_date'], searches=[],
        search_decision={'status': 'no_search', 'reason': 'No supplemental query was executed; article reader is unsupported'})
    payload['date_policy'] = b['date_policy']
    payload = runner._controlled_fetch_payload(path, b, payload)
    payload['completed_at'] = None
    runner._store_readback_and_freeze(b, payload, cumulative_actual=runner._empty_tool_usage(), allow_unresolved=True)
    runner._write_result(path, exit_code=0, retryable=False, error='Article dependency unsupported',
                         execution_complete=True, full_coverage=False)
    stored = load_acquisition_batch(b['registry_database'], b['acquisition_batch_id'])
    assert stored['items'][0]['attempts'][0]['event_kind'] == 'unsupported'
    status = service.progress(started['run_id'])
    assert status['stage'] == 'completed_with_gaps'
    assert 'before_target_request' in status['items'][0]['error']
    assert status['search_decision']['status'] == 'no_search'
    assert 'No supplemental query' in status['search_decision']['reason']
    assert status['budget']['used']['fetch_attempts'] == 0


def test_expired_article_work_retains_precheck_gap_without_reader(tmp_path, monkeypatch):
    from test_issue112_acquisition import _item
    from climate_monitor import article_content_adapter as article
    import scripts.run_agent_acquisition as runner
    monkeypatch.setattr(article, 'fetch_article_content', lambda *args, **kwargs: pytest.fail('expired handler invoked'))
    b = binding(tmp_path)
    payload = runner._controlled_fetch_payload(tmp_path / 'attempt-1.json', b,
        {'items': [_item()]}, deadline=runner.time.monotonic() - 1)
    assert payload['items'][0]['processing_status'] == 'failed'
    assert payload['items'][0]['evidence']['attempts'][0]['event_kind'] == 'precheck'


@pytest.mark.parametrize('tool', ['web_search', 'web_extract', 'browser_exec'])
def test_hermes_deadline_blocks_each_tool_without_dispatch(tmp_path, monkeypatch, tool):
    import climate_monitor.request_budget as module
    monkeypatch.setattr(module.time, 'time', lambda: 100.0)
    ledger = budget(tmp_path, runtime=10)
    monkeypatch.setattr(module.time, 'time', lambda: 110.0)
    directive = module.hook_decision(ledger, {'hook_event_name': 'pre_tool_call',
        'tool_name': tool, 'tool_input': {'query': 'climate', 'url': 'https://example.test/'},
        'session_id': 'test', 'extra': {'tool_call_id': '1'}})
    assert directive['action'] == 'block'
    assert ledger.usage()['fetch_attempts'] == ledger.usage()['search_attempts'] == 0
    assert ledger.events()[-1]['event_kind'] == 'precheck'


def test_incompatible_hermes_installation_never_launches_attempt(tmp_path, monkeypatch):
    import scripts.run_agent_acquisition as runner
    b = binding(tmp_path)
    b['provider'] = 'openai-codex'
    path = tmp_path / 'attempt-1.json'
    path.write_text(json.dumps(b))
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('incompatible attempt launched'))
    with pytest.raises(ValueError, match='Hermes Python'):
        runner._invoke_hermes(['/bin/true'], tmp_path/'response.txt', path, b, runner.time.monotonic()+60)


def test_openai_api_credential_reaches_hermes_and_report_processes(tmp_path, monkeypatch):
    import subprocess
    import scripts.run_agent_acquisition as runner

    exact = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=Path(__file__).resolve().parents[1], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    monkeypatch.setenv("OPENAI_API_KEY", "provider-credential")
    monkeypatch.setenv("RELOAD_TOKEN", "must-not-leak")
    b = binding(tmp_path)
    b.update(provider="openai-api", model="test-model", repository_commit_sha=exact)
    b["report_inputs"] = {
        key: str(tmp_path / key)
        for key in (
            "acquisition_batch", "web_listening_manifest", "pillar_b_artifact",
            "staging_dir", "state_dir", "source_dir", "wiki_dir",
        )
    }
    path = tmp_path / "attempt-1.json"
    path.write_text(json.dumps(b))
    environments = {}

    def install(command, binding_path, supplied, environment):
        environments["hermes-hook"] = environment
        return environment, tmp_path

    class Process:
        pid = 123

        def poll(self):
            return 0

        def wait(self):
            return 0

    def popen(*args, **kwargs):
        environments["hermes-process"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(runner, "install_hooks", install)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    assert runner._invoke_hermes(
        ["hermes"], tmp_path / "response.txt", path, b,
        runner.time.monotonic() + 60,
    ) == 0

    def run(command, **kwargs):
        environments["report"] = kwargs["env"]
        assert command[command.index("--repository-commit-sha") + 1] == exact
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", run)
    assert runner._run_report(path, b) == 0
    for environment in environments.values():
        assert environment["OPENAI_API_KEY"] == "provider-credential"
        assert "RELOAD_TOKEN" not in environment


def _managed_attempt(tmp_path, monkeypatch, *, source_keys=None, budget_overrides=None):
    from test_issue94_management_console import _store, _definition
    from climate_monitor.management import ManagementService

    monkeypatch.setenv("CLIMATE_MANAGED_STATE_DIR", str(tmp_path / "managed-state"))
    monkeypatch.setenv("CLIMATE_MANAGED_SOURCE_DIR", str(tmp_path / "managed-sources"))
    monkeypatch.setenv("CLIMATE_MANAGED_WIKI_DIR", str(tmp_path / "managed-wiki"))
    store = _store(tmp_path)
    definition = _definition(tmp_path)
    if source_keys is not None:
        definition["parameters"]["source_keys"] = source_keys
    if budget_overrides is not None:
        definition["parameters"]["budgets"].update(budget_overrides)
    store.save(definition, actor="operator")
    service = ManagementService(
        store=store, runtime_root=tmp_path / "runs", launcher=lambda binding: 4321,
    )
    started = service.start(trigger="manual")
    b = service.binding(started["run_id"])
    path = service._run_dir(started["run_id"]) / "attempt-1.json"
    path.write_text(json.dumps(b))
    return service, b, path


def _empty_agent_payload(binding, *, reason):
    return {
        "schema_version": "pre-report-acquisition-batch.v1",
        "batch_id": binding["acquisition_batch_id"],
        "report_date": binding["report_date"],
        "date_policy": binding["date_policy"],
        "started_at": binding["created_at"],
        "completed_at": binding["created_at"],
        "search_decision": {"status": "no_search", "reason": reason},
        "searches": [],
        "items": [],
    }


def test_primary_hermes_failure_retains_sanitized_process_error(tmp_path, monkeypatch):
    import scripts.run_agent_acquisition as runner

    service, b, path = _managed_attempt(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/false")
    monkeypatch.setattr(runner, "_controlled_site_context", lambda binding: {
        "status": "completed", "source_results": [], "attempts": [], "candidates": [],
    })

    def trusted(binding, *, allow_missing_session=False):
        if allow_missing_session:
            return []
        raise ValueError("Hermes did not persist the bound acquisition session")

    def invoke(command, response_path, binding_path, binding, deadline):
        response_path.write_text(
            "No usable credentials found for provider 'openai-api'. "
            "Set OPENAI_API_KEY. OPENAI_API_KEY=sk-test-secret"
        )
        return 78

    monkeypatch.setattr(runner, "_trusted_tool_events", trusted)
    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    exit_code = runner._execute_locked(path)
    status = service.progress(b["run_id"])
    result = json.loads(path.with_name("attempt-1-result.json").read_text())
    for value in (status, result):
        assert "No usable credentials found for provider 'openai-api'" in value["error"]
        assert "OPENAI_API_KEY=[REDACTED]" in value["error"]
        assert "sk-test-secret" not in value["error"]
        assert "did not persist the bound acquisition session" not in value["error"]
    assert exit_code == 78


@pytest.mark.parametrize(
    ("exit_code", "detail"),
    [
        (78, "No usable credentials found for provider 'openai-api'"),
        (124, "Timed out before Hermes could persist its session"),
    ],
)
def test_failed_hermes_without_session_database_retains_process_error(
    tmp_path, monkeypatch, exit_code, detail,
):
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    import scripts.run_agent_acquisition as runner

    service, b, path = _managed_attempt(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/false")
    monkeypatch.setattr(runner, "_controlled_site_context", lambda binding: {
        "status": "completed", "source_results": [], "attempts": [], "candidates": [],
    })

    def invoke(command, response_path, binding_path, binding, deadline):
        attempt_home(binding).mkdir(parents=True, exist_ok=True)
        response_path.write_text(f"{detail}. OPENAI_API_KEY=sk-missing-db-secret")
        return exit_code

    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    assert runner._execute_locked(path) == exit_code
    status = service.progress(b["run_id"])
    result = json.loads(path.with_name("attempt-1-result.json").read_text())
    expected_root = (
        "Hermes acquisition exceeded the bound runtime"
        if exit_code == 124
        else f"Hermes acquisition process exited with {exit_code}"
    )
    for value in (status, result):
        assert expected_root in value["error"]
        assert detail in value["error"]
        assert "OPENAI_API_KEY=[REDACTED]" in value["error"]
        assert "sk-missing-db-secret" not in value["error"]
        assert "durable session database is unavailable" not in value["error"]


def test_structured_provider_credentials_are_redacted_from_result_and_progress(
    tmp_path, monkeypatch,
):
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    import scripts.run_agent_acquisition as runner

    service, b, path = _managed_attempt(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/false")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-fake-bare-environment-value")
    monkeypatch.setattr(runner, "_controlled_site_context", lambda binding: {
        "status": "completed", "source_results": [], "attempts": [], "candidates": [],
    })
    fake_values = (
        "unit-fake-json-value",
        "unit-fake-python-value",
        "unit-fake-lower-api-value",
        "unit-fake-token-value",
        "unit-fake-secret-value",
        "unit-fake-password-value",
        "unit-fake-bare-environment-value",
    )

    def invoke(command, response_path, binding_path, binding, deadline):
        attempt_home(binding).mkdir(parents=True, exist_ok=True)
        response_path.write_text(
            "No usable credentials found for provider 'openai-api'. "
            '{"OPENAI_API_KEY": "unit-fake-json-value"} '
            "{'ANTHROPIC_API_KEY': 'unit-fake-python-value'} "
            'api_key="unit-fake-lower-api-value" '
            '{"token": "unit-fake-token-value"} '
            "{'Secret': 'unit-fake-secret-value'} "
            "password='unit-fake-password-value' "
            "credential unit-fake-bare-environment-value"
        )
        return 78

    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    assert runner._execute_locked(path) == 78
    result = json.loads(path.with_name("attempt-1-result.json").read_text())
    status = service.progress(b["run_id"])
    for value in (result, status):
        assert "No usable credentials found for provider 'openai-api'" in value["error"]
        assert value["error"].count("[REDACTED]") >= len(fake_values)
        assert not any(fake_value in value["error"] for fake_value in fake_values)


def test_successful_hermes_path_keeps_missing_database_validation_strict(tmp_path):
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    import scripts.run_agent_acquisition as runner

    b = binding(tmp_path)
    attempt_home(b).mkdir(parents=True)
    with pytest.raises(ValueError, match="durable session database is unavailable"):
        runner._trusted_tool_events(b)
    assert runner._trusted_tool_events(b, allow_missing_session=True) == []


def test_feedback_hermes_failure_retains_sanitized_process_error(tmp_path, monkeypatch):
    import scripts.run_agent_acquisition as runner

    service, b, path = _managed_attempt(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/false")
    monkeypatch.setattr(runner, "_controlled_site_context", lambda binding: {
        "status": "completed", "source_results": [], "attempts": [], "candidates": [],
    })
    missing = {"url": "https://example.test/article", "source": "example",
               "processing_status": "failed", "evidence": {"attempts": []}}
    payload = {"items": [missing], "searches": []}
    calls = []

    def invoke(command, response_path, binding_path, binding, deadline):
        calls.append(response_path)
        if len(calls) == 1:
            response_path.write_text("{}")
            return 0
        response_path.write_text(
            "Adaptive provider failed. OPENAI_API_KEY=sk-feedback-secret"
        )
        return 79

    def trusted(binding, *, allow_missing_session=False):
        if len(calls) == 2 and not allow_missing_session:
            raise ValueError("Hermes did not persist the bound acquisition session")
        return []

    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    monkeypatch.setattr(runner, "_extract_envelope", lambda response: {
        "acquisition_batch": payload,
    })
    monkeypatch.setattr(runner, "_validate_agent_payload", lambda binding, value, *args: value)
    monkeypatch.setattr(runner, "_validate_site_claims", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_controlled_fetch_payload", lambda *args, **kwargs: (payload, []))
    monkeypatch.setattr(runner, "_trusted_tool_events", trusted)
    monkeypatch.setattr(runner, "_store_readback_and_freeze", lambda *args, **kwargs: {})
    monkeypatch.setattr(runner, "_write_report_inputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "climate_registry.acquisition.readback_source_outcomes",
        lambda database, value: value["source_outcomes"],
    )
    exit_code = runner._execute_locked(path)
    status = service.progress(b["run_id"])
    result = json.loads(path.with_name("attempt-1-result.json").read_text())
    for value in (status, result):
        assert "Adaptive provider failed" in value["error"]
        assert "OPENAI_API_KEY=[REDACTED]" in value["error"]
        assert "sk-feedback-secret" not in value["error"]
        assert "did not persist the bound acquisition session" not in value["error"]
    assert exit_code == 79


def test_primary_nonzero_exit_keeps_root_and_charges_incomplete_transcript(
    tmp_path, monkeypatch,
):
    from test_issue94_management_console import _write_hermes_tool_events
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    from climate_monitor.request_budget import RequestBudget, ledger_path
    import scripts.run_agent_acquisition as runner

    service, b, path = _managed_attempt(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/false")
    monkeypatch.setattr(runner, "_controlled_site_context", lambda binding: {
        "status": "completed", "source_results": [], "attempts": [], "candidates": [],
    })

    def invoke(command, response_path, binding_path, binding, deadline):
        ledger = RequestBudget(ledger_path(binding), binding)
        ledger.claim(
            "web_search", "current climate risk",
            call_id="1:session-1:incomplete-search", results=2,
        )
        _write_hermes_tool_events(attempt_home(binding), binding, [{
            "tool": "web_search", "tool_call_id": "incomplete-search",
            "arguments": {"query": "current climate risk", "num_results": 2},
            "result": {"error": "provider stopped before post hook"},
        }])
        response_path.write_text("PRIMARY PROVIDER ROOT: upstream authentication failed")
        return 78

    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    assert runner._execute_locked(path) == 78

    status = service.progress(b["run_id"])
    result = json.loads(path.with_name("attempt-1-result.json").read_text())
    provenance = json.loads(path.with_name("attempt-1-tool-provenance.json").read_text())
    for value in (status, result):
        assert "Hermes acquisition process exited with 78" in value["error"]
        assert "PRIMARY PROVIDER ROOT: upstream authentication failed" in value["error"]
        assert "transcript lacks durable completion" not in value["error"]
    assert provenance["cumulative_actual"]["search_attempts"] == 1
    assert provenance["cumulative_actual"]["search_results"] == 0
    assert provenance["cumulative_actual"]["runtime_seconds"] > 0
    assert provenance["events"] == []
    assert len(provenance["request_events"]) == 1
    assert provenance["request_events"][0]["status"] == "reserved_or_uncertain"
    assert provenance["request_events"][0].get("completed") is not True


def test_feedback_nonzero_exit_keeps_root_and_charges_incomplete_transcript(
    tmp_path, monkeypatch,
):
    import sqlite3

    from test_issue94_management_console import _write_hermes_tool_events
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    from climate_monitor.request_budget import RequestBudget, ledger_path
    import scripts.run_agent_acquisition as runner

    service, b, path = _managed_attempt(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/false")
    monkeypatch.setattr(runner, "_controlled_site_context", lambda binding: {
        "status": "completed", "source_results": [], "attempts": [], "candidates": [],
    })
    missing = {"url": "https://example.test/article", "source": "example",
               "processing_status": "failed", "evidence": {"attempts": []}}
    payload = {"items": [missing], "searches": []}
    calls = []

    def invoke(command, response_path, binding_path, binding, deadline):
        calls.append(response_path)
        if len(calls) == 1:
            _write_hermes_tool_events(attempt_home(binding), binding, [])
            response_path.write_text("{}")
            return 0
        ledger = RequestBudget(ledger_path(binding), binding)
        ledger.claim(
            "web_extract", "https://example.test/article",
            call_id="1:session-1:incomplete-extract",
        )
        call = {"id": "incomplete-extract", "function": {
            "name": "web_extract",
            "arguments": json.dumps({"url": "https://example.test/article"}),
        }}
        connection = sqlite3.connect(attempt_home(binding) / "state.db")
        try:
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, "session-1", "assistant", None, None, json.dumps([call]), None),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (2, "session-1", "tool", "incomplete-extract", "web_extract", None,
                 json.dumps({"error": "provider stopped before post hook"})),
            )
            connection.commit()
        finally:
            connection.close()
        response_path.write_text("ADAPTIVE PROVIDER ROOT: upstream authentication failed")
        return 79

    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    monkeypatch.setattr(runner, "_extract_envelope", lambda response: {
        "acquisition_batch": payload,
    })
    monkeypatch.setattr(runner, "_validate_agent_payload", lambda binding, value, *args: value)
    monkeypatch.setattr(runner, "_validate_site_claims", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_controlled_fetch_payload", lambda *args, **kwargs: (payload, []))

    assert runner._execute_locked(path) == 79
    status = service.progress(b["run_id"])
    result = json.loads(path.with_name("attempt-1-result.json").read_text())
    provenance = json.loads(path.with_name("attempt-1-tool-provenance.json").read_text())
    for value in (status, result):
        assert "Hermes adaptive feedback process exited with 79" in value["error"]
        assert "ADAPTIVE PROVIDER ROOT: upstream authentication failed" in value["error"]
        assert "transcript lacks durable completion" not in value["error"]
    assert provenance["cumulative_actual"]["fetch_attempts"] == 1
    assert provenance["cumulative_actual"]["runtime_seconds"] > 0
    assert provenance["events"] == []
    assert len(provenance["request_events"]) == 1
    assert provenance["request_events"][0]["status"] == "reserved_or_uncertain"
    assert provenance["request_events"][0].get("completed") is not True


def test_primary_zero_exit_keeps_incomplete_transcript_validation_strict(
    tmp_path, monkeypatch,
):
    from test_issue94_management_console import _write_hermes_tool_events
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    from climate_monitor.request_budget import RequestBudget, ledger_path
    import scripts.run_agent_acquisition as runner

    service, b, path = _managed_attempt(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/true")
    monkeypatch.setattr(runner, "_controlled_site_context", lambda binding: {
        "status": "completed", "source_results": [], "attempts": [], "candidates": [],
    })

    def invoke(command, response_path, binding_path, binding, deadline):
        ledger = RequestBudget(ledger_path(binding), binding)
        ledger.claim(
            "web_search", "current climate risk",
            call_id="1:session-1:incomplete-search", results=1,
        )
        _write_hermes_tool_events(attempt_home(binding), binding, [{
            "tool": "web_search", "tool_call_id": "incomplete-search",
            "arguments": {"query": "current climate risk", "num_results": 1},
            "result": {"results": []},
        }])
        response_path.write_text("{}")
        return 0

    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    monkeypatch.setattr(runner, "_extract_envelope", lambda response: {
        "acquisition_batch": _empty_agent_payload(
            b, reason="No supplemental query was needed",
        ),
    })
    assert runner._execute_locked(path) == 65
    status = service.progress(b["run_id"])
    result = json.loads(path.with_name("attempt-1-result.json").read_text())
    for value in (status, result):
        assert "Trusted acquisition validation failed" in value["error"]
        assert "Hermes tool transcript lacks durable completion" in value["error"]


def _exercise_blocked_search_precheck(
    tmp_path, monkeypatch, *, num_results, expected_reason, result_field="num_results",
):
    from test_issue94_management_console import _write_hermes_tool_events
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    from climate_monitor.request_budget import RequestBudget, hook_decision, ledger_path
    from climate_registry.acquisition import load_acquisition_batch
    import scripts.run_agent_acquisition as runner

    service, b, path = _managed_attempt(
        tmp_path,
        monkeypatch,
        budget_overrides={"search_results": 5} if num_results is None else None,
    )
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/true")
    sends = []
    seed_runtime(monkeypatch, sends)

    def invoke(command, response_path, binding_path, binding, deadline):
        ledger = RequestBudget(ledger_path(binding), binding)
        result_limit = (
            int(binding["budgets"]["search_results"]) + 1
            if num_results is None else num_results
        )
        blocked = hook_decision(ledger, {
            "hook_event_name": "pre_tool_call",
            "tool_name": "web_search",
            "tool_input": {
                "query": "current climate risk",
                result_field: result_limit,
            },
            "session_id": "session-1",
            "extra": {"tool_call_id": "blocked-search"},
        })
        assert blocked == {
            "action": "block",
            "message": expected_reason,
        }
        _write_hermes_tool_events(attempt_home(binding), binding, [{
            "tool": "web_search",
            "tool_call_id": "blocked-search",
            "arguments": {"query": "current climate risk", result_field: result_limit},
            "result": {"error": expected_reason},
        }])
        response_path.write_text(json.dumps({
            "acquisition_batch": _empty_agent_payload(
                binding, reason="The model says no supplemental query was needed",
            ),
        }))
        return 0

    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    monkeypatch.setattr(
        runner, "_run_report", lambda *args: pytest.fail("precheck gap must block report"),
    )
    assert runner._execute_locked(path) == 0

    stored = load_acquisition_batch(b["registry_database"], b["acquisition_batch_id"])
    status = service.progress(b["run_id"])
    result = json.loads(path.with_name("attempt-1-result.json").read_text())
    acquisition = json.loads(path.with_name("attempt-1-acquisition.json").read_text())
    for value in (stored, status["search_decision"], acquisition["search_decision"]):
        reason = value.get("no_search_reason", value.get("reason"))
        assert reason == expected_reason
        assert "model says" not in reason
    search_prechecks = [
        event for event in RequestBudget(ledger_path(b), b).events()
        if event["event_kind"] == "precheck" and event.get("tool") == "web_search"
    ]
    assert len(search_prechecks) == 1
    assert search_prechecks[0]["call_id"] == "1:session-1:blocked-search"
    assert search_prechecks[0]["url"] == "current climate risk"
    assert search_prechecks[0]["reason"] == expected_reason
    assert stored["searches"] == []
    assert stored["completed_at"] is None
    assert status["stage"] == "completed_with_gaps"
    assert status["budget"]["used"]["search_attempts"] == 0
    assert status["coverage"]["full_success"] is False
    assert result["full_coverage"] is False
    assert expected_reason in result["error"]
    assert not Path(b["frozen_report_input"]).exists()


def test_blocked_search_precheck_round_trips_truth_and_blocks_report(tmp_path, monkeypatch):
    _exercise_blocked_search_precheck(
        tmp_path,
        monkeypatch,
        num_results=None,
        expected_reason="search result budget precheck blocked request",
    )


@pytest.mark.parametrize("num_results", [0, "five"], ids=["zero", "non-integer"])
@pytest.mark.parametrize("result_field", ["num_results", "limit"])
def test_invalid_search_result_limit_round_trips_truth_and_blocks_report(
    tmp_path, monkeypatch, num_results, result_field,
):
    _exercise_blocked_search_precheck(
        tmp_path,
        monkeypatch,
        num_results=num_results,
        result_field=result_field,
        expected_reason="invalid search result limit",
    )


@pytest.mark.parametrize("result_field", ["num_results", "limit"])
def test_per_call_search_result_limit_round_trips_truth_and_blocks_report(
    tmp_path, monkeypatch, result_field,
):
    _exercise_blocked_search_precheck(
        tmp_path,
        monkeypatch,
        num_results=11,
        result_field=result_field,
        expected_reason="search result limit exceeds per-call maximum of 10",
    )


def test_per_call_search_result_limit_allows_exact_maximum(tmp_path):
    from climate_monitor.request_budget import RequestBudget, hook_decision

    task_binding = binding(tmp_path)
    task_binding["budgets"]["search_results"] = 10
    ledger = RequestBudget(tmp_path / "request-budget.json", task_binding)

    assert hook_decision(ledger, {
        "hook_event_name": "pre_tool_call",
        "tool_name": "web_search",
        "tool_input": {"query": "current climate risk", "limit": 10},
        "session_id": "session-1",
        "extra": {"tool_call_id": "ten-results"},
    }) == {}
    assert ledger.usage()["search_attempts"] == 1
    assert ledger.usage()["search_results_reserved"] == 10


def test_invalid_search_precheck_reconciles_transcript_and_blocks_call_reuse(
    tmp_path,
):
    from test_issue94_management_console import _write_hermes_tool_events
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    from climate_monitor.request_budget import hook_decision
    import scripts.run_agent_acquisition as runner

    b = binding(tmp_path)
    b["created_at"] = "2026-09-12T00:00:00Z"
    ledger = budget(tmp_path)
    payload = {
        "hook_event_name": "pre_tool_call",
        "tool_name": "web_search",
        "tool_input": {"query": "current climate risk", "num_results": 0},
        "session_id": "session-1",
        "extra": {"tool_call_id": "blocked-search"},
    }
    assert hook_decision(ledger, payload) == {
        "action": "block", "message": "invalid search result limit",
    }
    _write_hermes_tool_events(attempt_home(b), b, [{
        "tool": "web_search", "tool_call_id": "blocked-search",
        "arguments": payload["tool_input"],
        "result": {"error": "invalid search result limit"},
    }])
    assert runner._trusted_tool_events(b) == []

    payload["tool_input"]["num_results"] = 1
    assert hook_decision(ledger, payload) == {
        "action": "block", "message": "duplicate tool dispatch blocked",
    }
    payload.update(
        hook_event_name="post_tool_call",
        extra={"tool_call_id": "blocked-search", "status": "error", "result": "blocked"},
    )
    assert hook_decision(ledger, payload) == {}
    payload["extra"]["tool_call_id"] = "different-call"
    assert hook_decision(ledger, payload) == {}

    events = ledger.events()
    assert [event["event_kind"] for event in events] == ["precheck", "precheck"]
    assert {event["call_id"] for event in events} == {"1:session-1:blocked-search"}
    assert not any(event.get("completed") for event in events)
    assert ledger.usage()["search_attempts"] == 0
    assert ledger.usage()["search_results"] == 0
    assert runner._trusted_tool_events(b) == []


@pytest.mark.parametrize(
    ("case", "expected_reason", "expected_tool", "expected_target", "expected_call_id"),
    [
        ("unsupported", "unconfigured acquisition tool", "other_tool",
         "https://unsupported.test/", "1:session-1:blocked-tool"),
        ("missing-identity", "missing durable session/tool-call identity", "web_search",
         "current climate risk", None),
    ],
)
def test_other_hook_blocks_round_trip_to_final_report_gate(
    tmp_path, monkeypatch, case, expected_reason, expected_tool, expected_target,
    expected_call_id,
):
    from test_issue94_management_console import _write_hermes_tool_events
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    from climate_monitor.request_budget import RequestBudget, hook_decision, ledger_path
    from climate_registry.acquisition import load_acquisition_batch
    import scripts.run_agent_acquisition as runner

    service, b, path = _managed_attempt(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/true")
    seed_runtime(monkeypatch, [])

    def invoke(command, response_path, binding_path, binding, deadline):
        ledger = RequestBudget(ledger_path(binding), binding)
        payload = {
            "hook_event_name": "pre_tool_call",
            "tool_name": expected_tool,
            "tool_input": (
                {"url": expected_target} if case == "unsupported"
                else {"query": expected_target, "num_results": 5}
            ),
            "session_id": "session-1",
            "extra": ({"tool_call_id": "blocked-tool"} if case == "unsupported" else {}),
        }
        assert hook_decision(ledger, payload) == {
            "action": "block", "message": expected_reason,
        }
        transcript = ([{
            "tool": expected_tool, "tool_call_id": "blocked-tool",
            "arguments": payload["tool_input"], "result": {"error": expected_reason},
        }] if case == "unsupported" else [])
        _write_hermes_tool_events(attempt_home(binding), binding, transcript)
        response_path.write_text(json.dumps({
            "acquisition_batch": _empty_agent_payload(
                binding, reason="The model says no supplemental query was needed",
            ),
        }))
        return 0

    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    monkeypatch.setattr(
        runner, "_run_report", lambda *args: pytest.fail("hook precheck must block report"),
    )
    assert runner._execute_locked(path) == 0

    ledger = RequestBudget(ledger_path(b), b)
    prechecks = [event for event in ledger.events() if event["event_kind"] == "precheck"]
    assert len(prechecks) == 1
    assert prechecks[0]["tool"] == expected_tool
    assert prechecks[0]["url"] == expected_target
    assert prechecks[0].get("call_id") == expected_call_id
    assert ledger.usage()["search_attempts"] == 0
    stored = load_acquisition_batch(b["registry_database"], b["acquisition_batch_id"])
    status = service.progress(b["run_id"])
    result = json.loads(path.with_name("attempt-1-result.json").read_text())
    assert stored["completed_at"] is None
    assert status["stage"] == "completed_with_gaps"
    assert status["coverage"]["full_success"] is False
    assert result["full_coverage"] is False
    assert expected_reason in status["error"]
    assert expected_reason in result["error"]
    assert not Path(b["frozen_report_input"]).exists()


def test_identical_systemic_reads_stop_after_three_and_preserve_full_inventory(
    tmp_path, monkeypatch,
):
    from climate_monitor.management import default_task_definition
    import scripts.run_agent_acquisition as runner

    source_keys = default_task_definition()["parameters"]["source_keys"]
    service, b, path = _managed_attempt(
        tmp_path, monkeypatch, source_keys=source_keys,
    )
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/true")
    sends = []
    seed_runtime(monkeypatch, sends, outcomes=lambda url: "incomplete")
    monkeypatch.setattr(runner, "_trusted_tool_events", lambda *args, **kwargs: [])

    def invoke(command, response_path, binding_path, binding, deadline):
        response_path.write_text(json.dumps({
            "acquisition_batch": _empty_agent_payload(
                binding, reason="No supplemental search was executed",
            ),
        }))
        return 0

    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    monkeypatch.setattr(
        runner, "_run_report", lambda *args: pytest.fail("source gaps must block report"),
    )
    assert runner._execute_locked(path) == 0

    acquisition = json.loads(path.with_name("attempt-1-acquisition.json").read_text())
    status = service.progress(b["run_id"])
    result = json.loads(path.with_name("attempt-1-result.json").read_text())
    source_outcomes = acquisition["source_outcomes"]
    seed_outcomes = [
        outcome
        for row in source_outcomes
        for outcome in row["manifest"]["seed_outcomes"].values()
    ]
    assert len(sends) == 3
    assert len(source_outcomes) == 36
    assert len(seed_outcomes) == 116
    assert sum(row["event_kind"] == "network" for row in seed_outcomes) == 3
    assert sum(row["event_kind"] == "precheck" for row in seed_outcomes) == 113
    assert all(row["status"] == "failed" for row in source_outcomes)
    assert status["coverage"]["total_sources"] == 36
    assert status["coverage"]["incomplete_sources"] == 36
    for value in (status, result):
        assert "OSError: network temporarily unavailable" in value["error"]
    assert not Path(b["frozen_report_input"]).exists()


def test_policy_and_varied_site_failures_do_not_trigger_systemic_stop(tmp_path, monkeypatch):
    from climate_monitor import web_listening_adapter as adapter
    from climate_monitor.models import MonitorSource

    kinds = {
        "policy-a": "rejected",
        "different-a": OSError("host-specific failure A"),
        "different-b": OSError("host-specific failure B"),
        "policy-b": "rejected",
        "same-a": OSError("host-specific failure A"),
        "same-b": OSError("host-specific failure A"),
        "success": "success",
    }
    sources = [
        MonitorSource(key=key, abbreviation=key, full_name=key, url=f"https://{key}.test/")
        for key in kinds
    ]
    sends = []
    seed_runtime(
        monkeypatch, sends,
        outcomes=lambda url: kinds[url.split("//", 1)[1].split(".", 1)[0]],
    )
    _, _, evidence = adapter.collect_website_items_with_evidence(
        sources, state_dir=tmp_path / "seeds", budget=budget(tmp_path, fetch=20),
    )

    seed_outcomes = [
        outcome
        for row in evidence["source_results"]
        for outcome in row["manifest"]["seed_outcomes"].values()
    ]
    assert len(evidence["source_results"]) == len(sources)
    assert len(seed_outcomes) == len(sources)
    assert len(sends) == 5
    assert not any(row["event_kind"] == "precheck" for row in seed_outcomes)
    assert evidence["systemic_error"] is None


def test_reused_success_receipt_resets_durable_systemic_failure_sequence(
    tmp_path, monkeypatch,
):
    from climate_monitor import web_listening_adapter as adapter
    from climate_monitor.models import MonitorSource

    keys = ("fail-1", "reused-success", "fail-2", "fail-3", "fail-4", "fail-5")
    sources = [
        MonitorSource(key=key, abbreviation=key, full_name=key, url=f"https://{key}.test/")
        for key in keys
    ]
    ledger = budget(tmp_path, fetch=30)
    ledger.save_receipt("seed:reused-success:https://reused-success.test/", {
        "status": "success",
        "event_kind": "source",
        "candidates": [],
        "checkpoint": {"content_hash": "a" * 64, "links": []},
        "candidate_urls": [],
        "observed_at": "2026-09-12T00:00:00+00:00",
    })
    sends = []
    seed_runtime(monkeypatch, sends, outcomes=lambda url: "incomplete")
    _, _, evidence = adapter.collect_website_items_with_evidence(
        sources, state_dir=tmp_path / "seeds", budget=ledger,
    )

    seed_outcomes = [
        outcome
        for row in evidence["source_results"]
        for outcome in row["manifest"]["seed_outcomes"].values()
    ]
    assert sends == [
        "https://fail-1.test/", "https://fail-2.test/",
        "https://fail-3.test/", "https://fail-4.test/",
    ]
    assert [row["event_kind"] for row in seed_outcomes] == [
        "network", "source", "network", "network", "network", "precheck",
    ]
    assert len(evidence["source_results"]) == len(sources)
    assert evidence["systemic_error"] == "OSError: network temporarily unavailable"
    state = json.loads((tmp_path / "request-budget.json").read_text())
    assert state["systemic_read_failure"] == {
        "signature": "OSError: network temporarily unavailable",
        "count": 3,
        "root_error": "OSError: network temporarily unavailable",
        "stopped": True,
    }


def test_systemic_failure_sequence_resumes_and_third_failure_stops(
    tmp_path, monkeypatch,
):
    from climate_monitor import web_listening_adapter as adapter
    from climate_monitor.models import MonitorSource
    from climate_monitor.request_budget import RequestBudget

    sources = [
        MonitorSource(key=f"seed-{index}", abbreviation=f"seed-{index}",
                      full_name=f"seed-{index}", url=f"https://seed-{index}.test/")
        for index in range(1, 7)
    ]
    sends = []
    first = budget(tmp_path, fetch=30)
    seed_runtime(monkeypatch, sends, interrupt_at=2, outcomes=lambda url: "incomplete")
    with pytest.raises(KeyboardInterrupt, match="simulated worker crash"):
        adapter.collect_website_items_with_evidence(
            sources, state_dir=tmp_path / "seeds", budget=first,
        )

    resumed = RequestBudget(
        tmp_path / "request-budget.json",
        binding(tmp_path, fetch=30, attempt=2),
    )
    seed_runtime(monkeypatch, sends, outcomes=lambda url: "incomplete")
    _, _, evidence = adapter.collect_website_items_with_evidence(
        sources, state_dir=tmp_path / "seeds", budget=resumed,
    )
    seed_outcomes = [
        outcome
        for row in evidence["source_results"]
        for outcome in row["manifest"]["seed_outcomes"].values()
    ]

    assert sends == [
        "https://seed-1.test/", "https://seed-2.test/", "https://seed-1.test/",
    ]
    assert [row["event_kind"] for row in seed_outcomes] == [
        "network", "precheck", "precheck", "precheck", "precheck", "precheck",
    ]
    assert len(evidence["source_results"]) == len(sources)
    assert evidence["systemic_error"] == "OSError: network temporarily unavailable"
    state = json.loads((tmp_path / "request-budget.json").read_text())
    assert state["systemic_read_failure"] == {
        "signature": "OSError: network temporarily unavailable",
        "count": 3,
        "root_error": "OSError: network temporarily unavailable",
        "stopped": True,
    }


def test_finished_systemic_stop_rearms_resume_and_preserves_receipts_and_usage(
    tmp_path, monkeypatch,
):
    from climate_monitor import web_listening_adapter as adapter
    from climate_monitor.models import MonitorSource
    from climate_monitor.request_budget import RequestBudget

    keys = ("success", "fail-1", "fail-2", "fail-3", "pending-1", "pending-2")
    sources = [
        MonitorSource(key=key, abbreviation=key, full_name=key,
                      url=f"https://{key}.test/")
        for key in keys
    ]
    sends = []
    first = budget(tmp_path, fetch=30)
    seed_runtime(
        monkeypatch, sends,
        outcomes=lambda url: (
            "success" if url == "https://success.test/" else "incomplete"
        ),
    )
    _, _, first_evidence = adapter.collect_website_items_with_evidence(
        sources, state_dir=tmp_path / "seeds", budget=first,
    )
    assert sends == [
        "https://success.test/", "https://fail-1.test/",
        "https://fail-2.test/", "https://fail-3.test/",
    ]
    assert first_evidence["systemic_error"] == "OSError: network temporarily unavailable"
    assert first.systemic_read_failure()["stopped"] is True
    first.finish()

    resumed = RequestBudget(
        tmp_path / "request-budget.json",
        binding(tmp_path, fetch=30, attempt=2),
    )
    assert resumed.systemic_read_failure() == {
        "signature": None, "count": 0, "root_error": None, "stopped": False,
    }
    resumed_sends = []
    seed_runtime(monkeypatch, resumed_sends)
    _, warnings, evidence = adapter.collect_website_items_with_evidence(
        sources, state_dir=tmp_path / "seeds", budget=resumed,
    )

    assert resumed_sends == [
        "https://fail-1.test/", "https://fail-2.test/", "https://fail-3.test/",
        "https://pending-1.test/", "https://pending-2.test/",
    ]
    assert evidence["full_success"] is True
    assert evidence["systemic_error"] is None
    assert warnings == []
    assert resumed.usage(attempt=1)["fetch_attempts"] == 4
    assert resumed.usage(attempt=2)["fetch_attempts"] == 5
    assert resumed.usage()["fetch_attempts"] == 9
    assert resumed.usage()["retries"] == 3


def test_finished_partial_systemic_sequence_rearms_before_resume(tmp_path, monkeypatch):
    from climate_monitor import web_listening_adapter as adapter
    from climate_monitor.models import MonitorSource
    from climate_monitor.request_budget import RequestBudget

    sources = [
        MonitorSource(key=f"fail-{index}", abbreviation=f"fail-{index}",
                      full_name=f"fail-{index}", url=f"https://fail-{index}.test/")
        for index in range(1, 3)
    ]
    first = budget(tmp_path, fetch=20)
    seed_runtime(monkeypatch, [], outcomes=lambda url: "incomplete")
    adapter.collect_website_items_with_evidence(
        sources, state_dir=tmp_path / "seeds", budget=first,
    )
    assert first.systemic_read_failure() == {
        "signature": "OSError: network temporarily unavailable",
        "count": 2,
        "root_error": "OSError: network temporarily unavailable",
        "stopped": False,
    }
    first.finish()

    resumed = RequestBudget(
        tmp_path / "request-budget.json",
        binding(tmp_path, fetch=20, attempt=2),
    )
    assert resumed.systemic_read_failure() == {
        "signature": None, "count": 0, "root_error": None, "stopped": False,
    }
    resumed_sends = []
    seed_runtime(monkeypatch, resumed_sends)
    adapter.collect_website_items_with_evidence(
        sources, state_dir=tmp_path / "seeds", budget=resumed,
    )
    assert resumed_sends == ["https://fail-1.test/", "https://fail-2.test/"]
    assert resumed.usage()["fetch_attempts"] == 4


def test_lower_fetch_override_is_frozen(tmp_path):
    from climate_monitor.management import default_task_definition, build_task_binding
    from climate_registry.persistent import initialize_registry
    definition = default_task_definition()
    definition['parameters']['budgets']['fetch_attempts'] = 1
    definition['runtime'].update(run_root=str(tmp_path), registry_database=str(tmp_path/'registry.db'))
    initialize_registry(tmp_path/'registry.db')
    value = build_task_binding(definition, task_version=1, run_id='small', attempt=1)
    assert value['budgets']['fetch_attempts'] == 1
    assert value['governed_gateway']['budget_limit'] == 1
    assert len(value['source_inventory']['records']) == 36


def test_shared_seed_url_is_not_another_sources_retry(tmp_path, monkeypatch):
    from climate_monitor import web_listening_adapter as adapter
    from climate_monitor.models import MonitorSource
    from climate_monitor.request_budget import RequestBudget
    b = binding(tmp_path)
    b['budgets']['retries_per_item'] = 0
    ledger = RequestBudget(tmp_path/'request-budget.json', b)
    sources = [MonitorSource(key=key, abbreviation=key, full_name=key,
                             url='https://example.test/') for key in ('first', 'second')]
    sends = []
    seed_runtime(monkeypatch, sends)
    _, warnings, evidence = adapter.collect_website_items_with_evidence(
        sources, state_dir=tmp_path/'seeds', budget=ledger)
    assert sends == ['https://example.test/'] * 2
    assert evidence['full_success'] and not warnings
    assert ledger.usage()['retries'] == 0


@pytest.mark.parametrize('event', ['pre_tool_call', 'post_tool_call'])
@pytest.mark.parametrize('field', ['session_id', 'tool_call_id'])
@pytest.mark.parametrize('value', [None, '', '   ', 123])
def test_review_hook_requires_both_durable_ids(event, field, value):
    from climate_monitor.request_budget import hook_decision
    invoked = []
    ledger = SimpleNamespace(attempt=1, claim=lambda *a, **kw: invoked.append('claim'),
                             complete_tool=lambda *a: invoked.append('complete'),
                             note=lambda *a, **kw: invoked.append(('note', a, kw)))
    payload = {'hook_event_name': event, 'tool_name': 'web_search',
               'session_id': 'session', 'extra': {'tool_call_id': 'call'},
               'tool_input': {'query': 'climate'}}
    if field == 'session_id':
        payload[field] = value
    else:
        payload['extra'][field] = value
    assert hook_decision(ledger, payload).get('action') == 'block'
    assert [entry for entry in invoked if entry == 'claim' or entry == 'complete'] == []
    assert invoked[0][0] == 'note'


@pytest.mark.parametrize('tool', ['web_search', 'web_extract', 'browser_exec'])
def test_review_transcript_requires_completed_admission(tmp_path, tool):
    from test_issue94_management_console import _write_hermes_tool_events
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    import scripts.run_agent_acquisition as runner
    b = binding(tmp_path)
    b['created_at'] = '2026-09-11T00:00:00Z'
    ledger = budget(tmp_path)
    result = {'results': [{'url': 'https://example.test/'}]}
    _write_hermes_tool_events(attempt_home(b), b, [
        {'tool': tool, 'tool_call_id': 'call', 'arguments': {}, 'result': result}])
    ledger.claim(tool, 'https://example.test/', call_id='1:session-1:call', results=1)
    with pytest.raises(ValueError, match='durable completion'):
        runner._trusted_tool_events(b)
    assert ledger.usage()['search_results'] == 0
    ledger.complete_tool('1:session-1:call', result, 'ok')
    assert len(runner._trusted_tool_events(b)) == 1
    if tool == 'web_search':
        assert ledger.usage()['search_results'] == 1
        assert ledger.usage()['search_results_reserved'] == 0


def test_review_failed_completion_is_not_marked_complete(tmp_path):
    ledger = budget(tmp_path)
    ledger.claim('web_search', 'climate', call_id='call', results=1)
    with pytest.raises(ValueError, match='exceeded'):
        ledger.complete_tool('call', {'results': [{'url': 'https://a.test/'}, {'url': 'https://b.test/'}]}, 'ok')
    assert not ledger.events()[0].get('completed')
    assert ledger.usage()['search_results_reserved'] == 1


def test_review_mixed_projection_matches_downstream_exports(tmp_path, monkeypatch):
    from climate_monitor import web_listening_adapter as adapter
    from climate_monitor.models import MonitorSource
    from scripts import run_agent_acquisition as runner, run_climate_monitor as monitor
    sources = [MonitorSource(key=key, abbreviation=key, full_name=key, url=f'https://{key}.test/')
               for key in ('success', 'rejected', 'incomplete')]
    seed_runtime(monkeypatch, [], outcomes=lambda url: url.split('//')[1].split('.')[0])
    _, _, context = adapter.collect_website_items_with_evidence(sources, state_dir=tmp_path/'seeds', budget=budget(tmp_path))
    b = {'source_inventory': {'records': [{'key': source.key} for source in sources]},
         'report_inputs': {key: str(tmp_path/(key+'.json')) for key in
                          ('acquisition_batch', 'web_listening_manifest', 'pillar_b_artifact')}}
    payload = {'items': [], 'report_date': '2026-09-07', 'date_policy': {},
               'search_decision': 'no_search', 'searches': []}
    runner._write_report_inputs(b, payload, context)
    outcomes = json.loads(Path(b['report_inputs']['acquisition_batch']).read_text())
    manifest_path = Path(b['report_inputs']['web_listening_manifest'])
    exports = json.loads(manifest_path.read_text())
    monitor._verify_collection_identity(outcomes, exports)
    assert len(exports) == 1 and len(outcomes) == 3
    diagnostics = json.loads(manifest_path.with_suffix('.diagnostics.json').read_text())
    assert {row['source']['source_id'] for row in diagnostics} == {'rejected', 'incomplete'}
    assert all(Path(row['artifact_path']).is_file() for row in context['source_results'])
