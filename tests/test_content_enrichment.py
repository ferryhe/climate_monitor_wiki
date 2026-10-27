"""G7 uses real source/writer fixtures and never approves its generated candidate."""
import json
import sqlite3
from copy import deepcopy

import pytest

from climate_registry.content_enrichment import generate, generate_dates, prepare, stage, sha
from climate_registry.enrichment_contract import validate_content_baselines, review_projection
from climate_registry.information_checks import check_targets, run_checks
from climate_registry.publication import snapshot_entity, stage_snapshot, stage_entities, _approve
from test_enrichment_contract import _published_a, _insert_body, ordered_source_check_fixture_clock
from test_owner_publication_dates import _seed
from test_article_content_dates import _manual
from test_owner_checks import _ingest
from test_verified_publication import _reader

BODY = "Flood losses reached 20 billion. Coastal insurance remains exposed.\n\nLate evidence: adaptation reduced flood claims by 30 percent."


def _author(request):
    text = request["article_body"]
    editorial = {"categories": ["Physical Risk"], "keywords": ["flood losses", "coastal insurance", "adaptation"],
        "evidence": {field: [{"value": value, "excerpt": text, "record_context": text,
            "reason": "The complete body discusses flood losses and insurance adaptation."} for value in values]
            for field, values in (("categories", ["Physical Risk"]), ("keywords", ["flood losses", "coastal insurance", "adaptation"]))}}
    if request["requested_outputs"] == ["editorial"]:
        return {"request_sha256": request["request_sha256"], "entity_type": "Article", "editorial": editorial}
    sections = request["sections"]
    statements = sections[:]
    from climate_registry.owner_checks import _dates
    dates = {}
    for selected in request.get("date_context", {}).get("required_dates", []):
        parsed = _dates(selected["text"])
        proof = {"date_kind": selected["date_kind"], "text": selected["text"],
            "value": parsed["start_date"], "source_date_precision": parsed["date_precision"]}
        if parsed["date_precision"] == "month":
            proof.update(value=parsed["start_date"] + "-15", date_normalization="month_midpoint")
        dates[selected["field"]] = proof
    return {"request_sha256": request["request_sha256"], "entity_type": "Article", "summary": " ".join(statements),
        "summary_support": "complete", "summary_items": [{"statement": value, "excerpt": value} for value in statements],
        "coverage": [{"section": number, "summary_items": [number]} for number in range(len(sections))], "editorial": editorial,
        "substantive_change": bool(request.get("previous")), "change_items": [{"note": "The source now reports 20 billion flood losses and reduced adaptation claims.",
            "basis": "body_comparison", "previous_excerpt": "Flood losses reached 10 billion.", "current_excerpt": sections[-1]}] if request.get("previous") else [],
        "date_evidence": dates, "language": "en"}


def _setup(tmp_path):
    database, approved = _published_a(tmp_path)
    with sqlite3.connect(database) as db:
        _insert_body(db, "body-b", BODY, "2026-10-16T00:00:00Z")
    return database, approved


def _target(database):
    return next(item for item in check_targets(database, "articles") if item["entity_kind"] == "article" and item["entity_id"] == "good")


def _generated(db, target, author=_author):
    return generate(db, target, author, provider="fixture-author", model="complete-v1")


def test_full_regeneration_itemized_note_and_pending_isolation(tmp_path):
    database, approved = _setup(tmp_path)
    before = _reader(database, tmp_path).article("good")
    with sqlite3.connect(database) as db:
        result = stage(db, _target(database), _generated(db, _target(database)), generated_at="2026-10-17T00:00:00Z")
        assert result["status"] == "pending_review", result
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (result["candidate_sha256"],)).fetchone()[0])
        bundle = snapshot["derived_display"]["content_update"]
        assert "30 percent" in bundle["summary"] and "Coastal insurance remains exposed" in bundle["summary"]
        assert bundle["first_observed_at"] == "2026-10-16T00:00:00Z"
        assert bundle["change_evidence"]["items"][0]["note"] == bundle["change_note"]
        assert snapshot["enrichment_scope"]["content_comparison_baseline"]["candidate_sha256"] == approved
        assert review_projection(snapshot)["content_authoring"] == snapshot["content_authoring"]
        validate_content_baselines(db, snapshot, result["candidate_sha256"])
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == approved
        assert not db.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=?", (result["candidate_sha256"],)).fetchone()
        stage_entities(db, article_ids={"good"})
        latest = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0]
        assert json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (latest,)).fetchone()[0])["content_authoring"] == snapshot["content_authoring"]
    assert _reader(database, tmp_path).article("good") == before


@pytest.mark.parametrize("mutation,reason", [("summary", "incomplete_current_summary"), ("coverage", "incomplete_current_summary"),
    ("evidence", "summary_statement"), ("binding", "request_binding"), ("taxonomy", "configured taxonomy"), ("keywords", "between 3 and 8")])
def test_incomplete_unbound_or_unsupported_generation_holds_without_old_labels(tmp_path, mutation, reason):
    database, _ = _setup(tmp_path)
    def broken(request):
        result = _author(request)
        if mutation == "summary": result["summary_support"] = "partial"
        if mutation == "coverage": result["coverage"].pop()
        if mutation == "evidence": result["summary_items"][-1]["excerpt"] = "not in body"
        if mutation == "binding": result["request_sha256"] = "0" * 64
        if mutation == "taxonomy": result["editorial"]["categories"] = ["Old unsupported label"]
        if mutation == "keywords": result["editorial"]["keywords"] = ["old"]
        return result
    with sqlite3.connect(database) as db:
        result = _generated(db, _target(database), broken)
    assert result["status"] == "held" and reason in result["reason"], result
    assert "record" not in result and "categories" not in result


def test_each_update_item_is_checked_not_only_the_first(tmp_path):
    database, _ = _setup(tmp_path)
    def broken(request):
        response = _author(request)
        response["change_items"].append({"note": "Unsupported second claim", "basis": "body_comparison",
            "previous_excerpt": "Invented old text", "current_excerpt": request["article_body"]})
        return response
    with sqlite3.connect(database) as db:
        with pytest.raises(ValueError, match="each change item"):
            stage(db, _target(database), _generated(db, _target(database), broken), generated_at="2026-10-17T00:00:00Z")


def test_missing_prior_body_uses_only_explicit_versioned_source_explanation(tmp_path, monkeypatch):
    database = _seed(tmp_path)
    with sqlite3.connect(database) as db:
        prior = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0]
        text = BODY + "\n\nRevision note: this edition adds adaptation claims results."
        _insert_body(db, "body-b", text, "2026-10-16T00:00:00Z")
    def author(request):
        response = _author(request)
        response["change_items"] = [{"note": "The publisher says this edition adds adaptation claims results.",
            "basis": "source_explanation", "text": request["sections"][-1]}]
        return response
    with sqlite3.connect(database) as db:
        result = stage(db, _target(database), _generated(db, _target(database), author), generated_at="2026-10-17T00:00:00Z")
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (result["candidate_sha256"],)).fetchone()[0])
        bundle = snapshot["derived_display"]["content_update"]
        assert bundle["schema_version"] == "article-content-update.source-explanation.v2"
        assert bundle["previous_body_sha256"] is None and bundle["previous_content_version_id"] is None
        assert bundle["previous_publication"]["candidate_sha256"] == prior
        assert bundle["previous_publication"]["summary"] == "Summary good"
        assert "cannot be confirmed" in bundle["change_note"]
        validate_content_baselines(db, snapshot, result["candidate_sha256"])
        from climate_registry.article_dates import validate_content_update
        bad = deepcopy(snapshot); bad["derived_display"]["content_update"]["schema_version"] = "article-content-update.v1"
        with pytest.raises(ValueError, match="distinct retained"):
            validate_content_update(bad)
        bad = deepcopy(snapshot); bad["derived_display"]["content_update"]["previous_publication"]["summary"] = "different old summary"
        with pytest.raises(ValueError): validate_content_baselines(db, bad, result["candidate_sha256"])
    from test_enrichment_contract import _native_contract_review
    from climate_registry.publication import export_public_snapshot
    from climate_registry.read_api import RegistryReader
    _native_contract_review(database, tmp_path, monkeypatch)
    public = _reader(database, tmp_path).article("good")
    assert public["summary"] == bundle["summary"]
    root = tmp_path/"exported"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    static = RegistryReader.from_public_export(root).article("good")
    assert static["content_update"] == public["content_update"]
    assert static["summary"] == bundle["summary"]


def test_human_date_publication_during_authoring_rebases_inheritance_only(tmp_path):
    database, approved = _setup(tmp_path)
    target = _target(database)
    with sqlite3.connect(database) as db:
        generated = _generated(db, target)
    manual = _ingest(database, tmp_path, _manual(database))
    with sqlite3.connect(database) as db:
        result = stage(db, target, generated, generated_at="2026-10-17T00:00:00Z")
        assert result["status"] == "pending_review", result
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (result["candidate_sha256"],)).fetchone()[0])
        assert snapshot["enrichment_scope"]["content_comparison_baseline"]["candidate_sha256"] == approved
        assert snapshot["enrichment_scope"]["approval_inheritance_baseline"]["candidate_sha256"] == manual["candidate_sha256"]
        assert snapshot["derived_display"]["publication_date"] == "2026-10-02"
        assert snapshot["derived_display"]["manual_date_overrides"]["publication_date"]["authorized_by"] == "owner"
        assert snapshot["content_authoring"]["output"]["summary"] == generated["record"]["output"]["summary"]


def test_late_b_cannot_overwrite_approved_c(tmp_path):
    database, _ = _setup(tmp_path)
    target = _target(database)
    with sqlite3.connect(database) as db:
        generated = _generated(db, target)
        _insert_body(db, "body-c", BODY.replace("20 billion", "30 billion"), "2026-10-18T00:00:00Z")
        current = snapshot_entity(db, "article", "good")
        current["derived_display"] = {"summary": "C approved content summary"}
        c = stage_snapshot(db, current)
        _approve(db, c, {"basis": "isolated C fixture publication"})
        result = stage(db, target, generated, generated_at="2026-10-19T00:00:00Z")
        assert result["status"] == "held" and "changed_during_authoring" in result["reason"]
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == c


@pytest.mark.parametrize("change", ["same", "date", "format"])
def test_retry_date_label_and_presentation_do_not_generate_updates(tmp_path, monkeypatch, change):
    from test_enrichment_contract import _native_contract_review
    database, _ = _setup(tmp_path)
    with sqlite3.connect(database) as db:
        stage(db, _target(database), _generated(db, _target(database)), generated_at="2026-10-17T00:00:00Z")
    _native_contract_review(database, tmp_path, monkeypatch)
    with sqlite3.connect(database) as db:
        old = prepare(db, _target(database))[1]["article_body"]
        body = old if change == "same" else old + "\nUpdated: 20 October 2026" if change == "date" else "**" + old + "**"
        if change != "same": _insert_body(db, "body-noop", body, "2026-10-20T00:00:00Z")
        result = _generated(db, _target(database), lambda request: pytest.fail("no semantic generation needed"))
        from climate_monitor.taxonomy import load_article_taxonomy
        assert result["status"] == "no-op" and result["reason"] == "unchanged_substantive_body"
        assert result["taxonomy_sha256"] == load_article_taxonomy().sha256
        assert result["request"]["article_body"] == body


def test_source_only_and_unretained_reader_body_hold_without_stale_taxonomy(tmp_path):
    database, _ = _setup(tmp_path)
    with sqlite3.connect(database) as db:
        held = _generated(db, _target(database), lambda request: {"request_sha256": request["request_sha256"], "entity_type": "source_only", "hold_reason": "directory page"})
        assert held == {"status": "held", "reason": "directory page"}
        held = generate(db, _target(database), _author, provider="fixture", model="fixture",
            reader={"status": "ok", "content_hash": sha("new unretained body")})
        assert held["status"] == "held" and "not_bound" in held["reason"]


def test_t1_production_path_calls_shared_author_and_retains_attempt(tmp_path):
    database, approved = _setup(tmp_path)
    calls = []
    def author(request): calls.append(request); return _author(request)
    def fetch(identity, url):
        return {"article_id": identity, "requested_url": url, "final_url": url, "status": "ok", "content": BODY,
            "content_ref": None, "content_hash": sha(BODY), "failure_reason": None}
    result = run_checks(database, kind="articles", backup_dir=tmp_path/"backups", occurrence_ids={"core:good"}, reverify_all=True,
        fetcher=fetch, verifier=lambda kind, fields, body: {"comparisons": {key: {"status": "supported"} for key in fields}},
        author=author, author_provider="fixture-author", author_model="complete-v1")
    assert len(calls) == 1 and result["content_enrichment"][0]["status"] == "pending_review", result
    with sqlite3.connect(database) as db:
        packet = json.loads(db.execute("SELECT packet_json FROM article_check_attempts WHERE run_id=?", (result["run_id"],)).fetchone()[0])
        assert packet["content_enrichment"]["record"]["output"]["summary"].endswith("30 percent.")
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == approved


def test_dates_uses_existing_extraction_schema_without_article_taxonomy(tmp_path):
    from test_issue136_meetings import _candidate
    body = "World Climate Summit 2027. June 10–12, 2027. Climate risk agenda."
    event = _candidate(location=None, organizer=None, online_url=None, raw_time_text=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None)
    target = {"entity_id": "existing-event", "source_url": "https://example.org/summit", "source_revision_sha256": "f" * 64,
        "fields": {"name": event["name"], "start_date": event["start_date"]}, "source_binding": {"event_id": "existing-event", "occurrence_id": "specific-2027-edition"}}
    def author(request):
        assert request["requested_outputs"] == ["Dates"] and "taxonomy" not in request
        assert request["target_fields"] == target["fields"] and request["source_binding"] == target["source_binding"]
        from climate_monitor.meetings import EXTRACTION_FIELDS, EVENT_TYPES, EVENT_STATUSES, DATE_PRECISIONS, DEADLINE_TYPES
        assert request["event_schema"] == {"fields": sorted(EXTRACTION_FIELDS), "event_types": sorted(EVENT_TYPES),
            "statuses": sorted(EVENT_STATUSES), "date_precisions": sorted(DATE_PRECISIONS), "deadline_types": sorted(DEADLINE_TYPES)}
        return {"request_sha256": request["request_sha256"], "entity_type": "Dates", "events": [event]}
    generated, actual = generate_dates(target, {"content": body, "content_hash": sha(body)}, author, provider="fixture", model="fixture")
    assert actual["name"] == event["name"] and actual["start_date"] == "2027-06-10"
    assert "categories" not in generated["output"] and "keywords" not in generated["output"]


@pytest.mark.parametrize("source", ["site", "search", "pdf"])
def test_three_real_source_writers_share_initial_article_semantics(tmp_path, monkeypatch, source):
    from test_issue112_acquisition import _batch, _item, _database
    from climate_registry.acquisition import store_acquisition_batch
    from climate_registry.publication import migrate_publication
    database = _database(tmp_path)
    migrate_publication(database, tmp_path/"backups", apply=True)
    url = "https://example.org/article"
    if source == "pdf":
        from test_pdf_intake import _coverage_layout, _source_decision, _material_bindings
        from climate_monitor.pdf_intake import import_pdf_reports, reconcile_source_coverage
        from climate_registry.pdf_intake import persist_pdf_intake
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        path = tmp_path/"source.pdf"; _coverage_layout(path)
        bundle = import_pdf_reports([path])
        coverage = bundle["source_coverage"][0]
        bindings = _material_bindings(coverage)
        reconcile_source_coverage(bundle, [_source_decision(coverage, "g7-material", bindings)])
        original_id = next(iter(coverage["observation_bindings"]))
        occurrence = next(item for article in bundle["articles"] for item in article["occurrences"] if item["occurrence_id"] == original_id)
        url = occurrence["raw_url"]
        persist_pdf_intake(database, tmp_path/"pdf-backups", bundle)
    item = _item(url=url, body=BODY, discovery_kind="site" if source == "site" else "search",
        discovery_ref="site-page" if source == "site" else "result-1", discovery_search_ref=None if source == "site" else "search-1")
    payload = _batch([item], searches=[] if source == "site" else None,
        search_decision={"status": "no_search", "reason": "website rotation"} if source == "site" else None)
    store_acquisition_batch(database, payload)
    if source == "pdf":
        # An explicit historical stored identity link is a prerequisite, never a G7 write.
        with sqlite3.connect(database) as db:
            core = db.execute("SELECT article_id FROM articles WHERE canonical_url=?", (url,)).fetchone()[0]
            db.execute("UPDATE pdf_intake_articles SET core_article_id=?,confirmation_basis='exact_url_eligible_detail' WHERE canonical_url=?", (core, url))
    targets = check_targets(database, "articles")
    target = next(item for item in targets if item["entity_kind"] == ("pdf_article" if source == "pdf" else "article") and item["source_url"] == url)
    with sqlite3.connect(database) as db:
        result = _generated(db, target)
        assert result["status"] == "generated", result
        staged = stage(db, target, result, generated_at="2026-10-17T00:00:00Z")
        assert staged["status"] == "pending_review", staged
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (staged["candidate_sha256"],)).fetchone()[0])
        assert snapshot["derived_display"]["summary"] == BODY.replace("\n\n", " ")
        assert snapshot["derived_display"]["categories"] == ["Physical Risk"]
        assert snapshot["derived_display"]["keywords"] == ["flood losses", "coastal insurance", "adaptation"]
        assert snapshot["enrichment_scope"]["scope"] == "initial_article_content"
        if source == "pdf": assert snapshot["tables"]["pdf_intake_article_occurrences"]
        else: assert snapshot["tables"]["acquisition_items"][0]["discovery_kind"] == source
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id=?", (snapshot["entity_id"],)).fetchone()[0] is None


def test_taxonomy_only_change_reuses_frozen_summary_and_note(tmp_path, monkeypatch):
    import climate_monitor.taxonomy as taxonomy
    database, _ = _setup(tmp_path)
    target = _target(database)
    with sqlite3.connect(database) as db:
        first = _generated(db, target)
        assert stage(db, target, first, generated_at="2026-10-17T00:00:00Z")["status"] == "pending_review"
    old = taxonomy.load_article_taxonomy()
    new = taxonomy._parse_article_taxonomy_bytes((old.definition_yaml + "\n# Current taxonomy documentation revision\n").encode())
    monkeypatch.setattr(taxonomy, "load_article_taxonomy", lambda *args: new)
    calls = []
    def author(request): calls.append(request); return _author(request)
    with sqlite3.connect(database) as db:
        result = _generated(db, target, author)
        assert result["status"] == "generated", result
        assert calls[0]["requested_outputs"] == ["editorial"]
        assert result["record"]["output"]["summary"] == first["record"]["output"]["summary"]
        assert result["record"]["output"]["change_items"] == first["record"]["output"]["change_items"]
        assert result["reused"] == ["summary", "changes", "dates"]
        from climate_registry.acquisition_review import digest
        inference = result["record"]["editorial_inference"]
        assert inference["response"]["request_sha256"] == digest(inference["request"])
        assert stage(db, target, result, generated_at="2026-10-18T00:00:00Z")["status"] == "pending_review"


def test_recapture_keeps_first_observation_and_reuses_generation(tmp_path):
    database, _ = _setup(tmp_path)
    target = _target(database)
    with sqlite3.connect(database) as db:
        first = stage(db, target, _generated(db, target), generated_at="2026-10-17T00:00:00Z")
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES('recapture','good','https://example.org/good','https://example.org/good','2026-10-20T00:00:00Z','success',200,'body-b')")
        generated = _generated(db, target, lambda request: pytest.fail("same body does not need regeneration"))
        result = stage(db, target, generated, generated_at="2026-10-20T00:00:00Z")
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (result["candidate_sha256"],)).fetchone()[0])
        assert snapshot["derived_display"]["content_update"]["first_observed_at"] == "2026-10-16T00:00:00Z"
        assert generated["reused"] == ["summary", "editorial", "changes", "dates"]
        assert generated["record"]["reused_from"]["candidate_sha256"] == first["candidate_sha256"]


def test_new_incomplete_capture_never_reuses_an_old_available_body(tmp_path):
    from climate_registry.capture import _insert_failed_fetch, FetchFailure
    database, _ = _setup(tmp_path)
    with sqlite3.connect(database) as db:
        target = _target(database)
        _insert_failed_fetch(db, article_id="good", requested_url=target["source_url"], fetched_at="2026-10-18T00:00:00Z",
            failure=FetchFailure("blocked_response", "No current body", http_status=403), id_factory=lambda kind: "latest-failed-fetch")
        held = _generated(db, target, lambda request: pytest.fail("old available content cannot substitute for a missing current body"))
        assert held["status"] == "held" and "latest_source_observation_has_no_complete_current_body" in held["reason"]
        current = generate(db, target, _author, provider="fixture", model="fixture", reader={"status": "ok", "content_hash": sha(BODY), "content": BODY})
        assert current["status"] == "held", current
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES('current-complete','good','https://example.org/good','https://example.org/good','2026-10-19T00:00:00Z','success',200,'body-b')")
        assert _generated(db, target)["status"] == "generated"


def test_source_change_during_authoring_requires_rebinding_even_with_identical_body(tmp_path):
    from climate_registry.capture import _insert_failed_fetch, FetchFailure
    database, approved = _setup(tmp_path)
    with sqlite3.connect(database) as db:
        target = _target(database)
        generated = _generated(db, target)
        _insert_failed_fetch(db, article_id="good", requested_url=target["source_url"], fetched_at="2026-10-18T00:00:00Z",
            failure=FetchFailure("blocked_response", "No current body", http_status=403), id_factory=lambda kind: "late-failed-fetch")
        result = stage(db, target, generated, generated_at="2026-10-18T00:00:00Z")
        assert result["status"] == "held" and "changed_during_authoring" in result["reason"]
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == approved


def test_new_search_snippet_holds_instead_of_claiming_old_body_unchanged(tmp_path):
    from test_issue112_acquisition import _batch, _item, _database
    from climate_registry.acquisition import store_acquisition_batch
    from climate_registry.publication import migrate_publication
    from climate_registry.content_enrichment import bind_current_body
    database = _database(tmp_path)
    migrate_publication(database, tmp_path/"backups", apply=True)
    store_acquisition_batch(database, _batch([_item(body=BODY)]))
    target = check_targets(database, "articles")[0]
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", target["entity_id"])
        bind_current_body(snapshot)
        snapshot["derived_display"] = {"summary": "Existing complete published summary"}
        old = stage_snapshot(db, snapshot)
        _approve(db, old, {"basis": "explicit existing body fixture"}, status="accepted_legacy")
    item = _item(body=BODY, status="no_content")
    item["discovered_at"] = "2026-09-11T08:00:00Z"
    item["evidence"].update(classification="snippet", fetched_at=item["discovered_at"], content=None, content_hash=None)
    payload = _batch([item], batch_id="snippet-batch")
    payload.update(started_at=item["discovered_at"], completed_at=None)
    store_acquisition_batch(database, payload)
    target = check_targets(database, "articles")[0]
    with sqlite3.connect(database) as db:
        result = _generated(db, target, lambda request: pytest.fail("search snippets cannot establish a current body"))
        assert result["status"] == "held" and "old_body_or_summary_cannot_substitute" in result["reason"]
        assert "record" not in result and "categories" not in result


def test_template_only_semantic_noop_is_separate_from_source_change(tmp_path):
    database, approved = _published_a(tmp_path)
    with sqlite3.connect(database) as db:
        old = prepare(db, _target(database))[1]["article_body"]
        _insert_body(db, "body-b", old + "\n\nNavigation: subscribe to alerts.", "2026-10-16T00:00:00Z")
    def author(request):
        return {"request_sha256": request["request_sha256"], "entity_type": "Article", "substantive_change": False,
            "change_items": [], "no_change_reason": "Only navigation/template wording changed.",
            "no_change_evidence": "Navigation: subscribe to alerts."}
    with sqlite3.connect(database) as db:
        generated = _generated(db, _target(database), author)
        assert generated["status"] == "no-op"
        assert stage(db, _target(database), generated, generated_at="2026-10-17T00:00:00Z")["status"] == "no-op"
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == approved


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
def test_missing_current_body_or_pdf_prerequisite_is_an_explicit_hold(tmp_path, source):
    database = _seed(tmp_path, source)
    target = next(item for item in check_targets(database, "articles") if item["entity_kind"] == ("article" if source == "native" else "pdf_article"))
    with sqlite3.connect(database) as db:
        result = _generated(db, target, lambda request: pytest.fail("incomplete input cannot be authored"))
        assert result["status"] == "held"
        assert "body" in result["reason"]
        assert "categories" not in result and "record" not in result


def test_t1_dates_production_writer_keeps_event_fields_and_pending_status(tmp_path):
    from test_issue136_meetings import _database, _candidate
    from climate_registry.publication import migrate_publication
    from climate_monitor.meetings import process_batch
    body = "World Climate Summit 2027. June 10–12, 2027. Climate risk agenda."
    database = _database(tmp_path, [body])
    migrate_publication(database, tmp_path/"backups", apply=True)
    event = _candidate(location=None, organizer=None, online_url=None, raw_time_text=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None)
    process_batch(database, "batch", prompt_text="fixture", prompt_version="v1", provider="fixture", model="fixture",
        extractor=lambda request: {"events": [event]})
    calls = []
    def author(request):
        calls.append(request)
        return {"request_sha256": request["request_sha256"], "entity_type": "Dates", "events": [event]}
    def fetch(identity, url):
        return {"article_id": identity, "requested_url": url, "final_url": url, "status": "ok", "content": body,
            "content_ref": None, "content_hash": sha(body), "failure_reason": None}
    result = run_checks(database, kind="meetings", backup_dir=tmp_path/"backups", reverify_all=True, fetcher=fetch,
        verifier=lambda kind, fields, body: {"comparisons": {key: {"status": "supported"} for key in fields}, "website_candidate": event},
        author=author, author_provider="fixture-author", author_model="complete-v1")
    assert calls and all("taxonomy" not in item for item in calls)
    assert all(item["target_fields"]["name"] == "World Climate Summit 2027" and item["source_binding"] for item in calls)
    assert result["content_enrichment"][0]["status"] == "generated_dates", result
    assert result["status"] == "complete" and result["unverified_count"] == 0, result
    with sqlite3.connect(database) as db:
        packet = json.loads(db.execute("SELECT packet_json FROM meeting_check_attempts WHERE run_id=?", (result["run_id"],)).fetchone()[0])
        assert packet["verification_status"] == "verified" and packet["error"] is None, packet
        assert packet["website_candidate"]["start_date"] == "2027-06-10"
        assert packet["website_candidate"]["name"] == "World Climate Summit 2027"
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='meeting'").fetchone()[0] is None


def test_pdf_dates_raw_author_output_reaches_verified_pending_writer(tmp_path, monkeypatch):
    from test_pdf_intake import _report_pdf, _source_decision
    from test_issue136_meetings import _candidate
    from climate_monitor.pdf_intake import import_pdf_reports, reconcile_source_coverage
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.persistent import initialize_registry
    from climate_registry.publication import migrate_publication
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path/"dates.pdf"; _report_pdf(path, calendar_date="10–12 June 2027")
    bundle = import_pdf_reports([path])
    item = bundle["calendar_items"][0]
    coverage = bundle["source_coverage"][0]
    bindings = [row for row in coverage["bindings"] if row["binding_id"] in coverage["observation_bindings"][item["occurrence_id"]]]
    reconcile_source_coverage(bundle, [_source_decision(coverage, "specific-event", bindings, observation_ids=[item["occurrence_id"]])])
    database = tmp_path/"registry.sqlite3"; initialize_registry(database)
    migrate_publication(database, tmp_path/"backups", apply=True)
    persist_pdf_intake(database, tmp_path/"backups", bundle)
    event = _candidate(name=item["name"], event_type="conference", location=None, organizer=None, online_url=None, raw_time_text=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None)
    body = item["name"] + ". June 10–12, 2027. Climate risk agenda."
    def fetch(identity, url):
        return {"article_id": identity, "requested_url": url, "final_url": url, "status": "ok", "content": body,
            "content_ref": None, "content_hash": sha(body), "failure_reason": None}
    result = run_checks(database, kind="meetings", backup_dir=tmp_path/"backups", occurrence_ids={item["occurrence_id"]}, reverify_all=True,
        fetcher=fetch, verifier=lambda kind, fields, body: {"comparisons": {key: {"status": "supported"} for key in fields}, "website_candidate": event},
        author=lambda request: {"request_sha256": request["request_sha256"], "entity_type": "Dates", "events": [event]},
        author_provider="fixture-author", author_model="complete-v1")
    assert result["status"] == "complete" and result["unverified_count"] == 0, result
    with sqlite3.connect(database) as db:
        packet = json.loads(db.execute("SELECT packet_json FROM meeting_check_attempts WHERE run_id=?", (result["run_id"],)).fetchone()[0])
        assert packet["verification_status"] == "verified" and packet["error"] is None, packet
        assert not db.execute("SELECT 1 FROM registry_publication WHERE published_candidate_sha256 IS NOT NULL").fetchone()


def test_published_same_body_taxonomy_change_only_restages_editorial(tmp_path, monkeypatch):
    import climate_monitor.taxonomy as taxonomy
    from test_enrichment_contract import _native_contract_review
    database, _ = _setup(tmp_path)
    target = _target(database)
    with sqlite3.connect(database) as db:
        first = stage(db, target, _generated(db, target), generated_at="2026-10-17T00:00:00Z")
    _native_contract_review(database, tmp_path, monkeypatch)
    with sqlite3.connect(database) as db:
        approved = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (first["candidate_sha256"],)).fetchone()[0])
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == first["candidate_sha256"]
        noop = _generated(db, target, lambda request: pytest.fail("unchanged approved taxonomy needs no new author"))
        from climate_registry.information_checks import _generation_complete
        assert _generation_complete(db, {**target, "content_enrichment": noop}, require_author=True)
    before = _reader(database, tmp_path).article("good")
    original = taxonomy.load_article_taxonomy()
    changed = taxonomy._parse_article_taxonomy_bytes(original.definition_yaml.replace("label: Physical Risk", "label: Catastrophe Exposure").encode())
    monkeypatch.setattr(taxonomy, "load_article_taxonomy", lambda *args: changed)
    with sqlite3.connect(database) as db:
        assert not _generation_complete(db, {**target, "content_enrichment": noop}, require_author=True)
    calls = []
    def author(request):
        calls.append(request)
        response = _author(request)
        response["editorial"]["categories"] = ["Catastrophe Exposure"]
        response["editorial"]["evidence"]["categories"][0]["value"] = "Catastrophe Exposure"
        response["editorial"]["keywords"][-1] = "adaptation claims"
        response["editorial"]["evidence"]["keywords"][-1]["value"] = "adaptation claims"
        return response
    with sqlite3.connect(database) as db:
        generated = _generated(db, target, author)
        assert generated["status"] == "generated", generated
        assert len(calls) == 1 and calls[0]["requested_outputs"] == ["editorial"]
        assert generated["record"]["output"]["summary"] == approved["derived_display"]["summary"]
        assert generated["record"]["output"]["change_items"] == approved["content_authoring"]["output"]["change_items"]
        staged = stage(db, target, generated, generated_at="2026-10-18T00:00:00Z")
        assert staged["status"] == "pending_review", staged
        actual = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (staged["candidate_sha256"],)).fetchone()[0])
        assert actual["derived_display"]["summary"] == approved["derived_display"]["summary"]
        assert actual["derived_display"]["content_update"] == approved["derived_display"]["content_update"]
        assert actual["derived_display"]["categories"] == ["Catastrophe Exposure"]
        assert actual["derived_display"]["keywords"][-1] == "adaptation claims"
        assert actual["enrichment_scope"]["operation_scope"] == "native_source_fields"
        validate_content_baselines(db, actual, staged["candidate_sha256"])
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == first["candidate_sha256"]
    assert _reader(database, tmp_path).article("good") == before


def test_dates_raw_input_contract_still_rejects_internal_normalization_keys():
    from test_issue136_meetings import _candidate
    body = "World Climate Summit 2027. June 10–12, 2027. Climate risk agenda."
    event = _candidate(location=None, organizer=None, online_url=None, raw_time_text=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None)
    event["_single_day_supported"] = False
    target = {"entity_id": "exact-event", "source_url": "https://example.org/event", "source_revision_sha256": "a" * 64}
    with pytest.raises(ValueError, match="unexpected or missing fields"):
        generate_dates(target, {"content": body, "content_hash": sha(body)},
            lambda request: {"request_sha256": request["request_sha256"], "entity_type": "Dates", "events": [event]}, provider="fixture", model="fixture")


@pytest.mark.parametrize("stage_hold", [False, True])
def test_two_resumes_keep_persisted_generation_or_staging_hold_until_real_retry(tmp_path, stage_hold):
    from climate_registry.capture import _insert_failed_fetch, FetchFailure
    database, _ = _setup(tmp_path)
    calls = []
    def author(request):
        calls.append(request)
        response = _author(request)
        if len(calls) == 1:
            if stage_hold:
                with sqlite3.connect(database) as db:
                    _insert_failed_fetch(db, article_id="good", requested_url=request["source_url"], fetched_at="2026-10-18T00:00:00Z",
                        failure=FetchFailure("blocked_response", "No current body", http_status=403), id_factory=lambda kind: "during-author-failure")
            else:
                response["summary_support"] = "partial"
        return response
    def fetch(identity, url):
        return {"article_id": identity, "requested_url": url, "final_url": url, "status": "ok", "content": BODY,
            "content_ref": None, "content_hash": sha(BODY), "failure_reason": None}
    options = {"kind": "articles", "backup_dir": tmp_path/"backups", "fetcher": fetch,
        "verifier": lambda kind, fields, body: {"comparisons": {key: {"status": "supported"} for key in fields}},
        "author": author, "author_provider": "fixture-author", "author_model": "complete-v1"}
    first = run_checks(database, **options, occurrence_ids={"core:good"}, reverify_all=True)
    assert first["status"] == "partial" and first["enrichment_failed_count"] == 1, first
    reason = first["content_enrichment"][0]["reason"]
    with sqlite3.connect(database) as db:
        frozen_packet = db.execute("SELECT packet_json FROM article_check_attempts WHERE run_id=?", (first["run_id"],)).fetchone()[0]
    for _ in range(2):
        resumed = run_checks(database, **options, resume_run_id=first["run_id"])
        assert resumed["status"] == "partial" and resumed["enrichment_failed_count"] == 1, resumed
        assert resumed["content_enrichment"][0]["status"] == "held" and resumed["content_enrichment"][0]["reason"] == reason
        assert len(calls) == 1
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT packet_json FROM article_check_attempts WHERE run_id=?", (first["run_id"],)).fetchone()[0] == frozen_packet
    if stage_hold:
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES('restored-current','good','https://example.org/good','https://example.org/good','2026-10-19T00:00:00Z','success',200,'body-b')")
    retried = run_checks(database, **options, retry_run_id=first["run_id"])
    assert retried["status"] == "complete" and retried["enrichment_failed_count"] == 0, retried
    assert len(calls) == 2 and retried["content_enrichment"][0]["status"] == "pending_review"


def test_existing_t1_cli_supplies_frozen_runtime_to_shared_author(tmp_path, monkeypatch):
    from scripts import check_information, run_meeting_extraction
    from climate_registry.acquisition_review import digest
    database, _ = _setup(tmp_path)
    binding = {"registry_database": str(database), "hermes_snapshot": {"sha256": "f" * 64}, "run_id": "fixture-run"}
    path = tmp_path/"binding.json"; path.write_text(json.dumps(binding))
    options = {}
    callback = lambda request: None
    def extractor(provider, model, path, **kwargs):
        assert (provider, model) == ("", "")
        assert kwargs == {"purpose": "content-enrichment", "acquisition_sha256": digest(binding)}
        return callback
    def checks(database, **kwargs):
        options.update(kwargs)
        return {"status": "complete", "item_count": 0, "completed_count": 0}
    monkeypatch.setattr(run_meeting_extraction, "_extractor", extractor)
    monkeypatch.setattr(check_information, "_runtime_identity", lambda *args: {"provider": "fixture-provider", "model": "fixture-model", "source": "climate-acquisition-fixture-run"})
    monkeypatch.setattr(check_information, "run_checks", checks)
    assert check_information.main(["--kind", "articles", "--database", str(database), "--backup-dir", str(tmp_path/"backups"),
        "--authoring-binding", str(path)]) == 0
    assert options["author"] is callback and options["author_provider"] == "fixture-provider"


@pytest.mark.parametrize("continuation", ["resume_run_id", "retry_run_id"])
def test_t1_continuation_cannot_change_frozen_author_runtime(tmp_path, continuation):
    database, _ = _setup(tmp_path)
    def fetch(identity, url):
        return {"article_id": identity, "requested_url": url, "final_url": url, "status": "ok", "content": BODY,
            "content_ref": None, "content_hash": sha(BODY), "failure_reason": None}
    options = {"kind": "articles", "backup_dir": tmp_path/"backups", "occurrence_ids": {"core:good"},
        "fetcher": fetch, "verifier": lambda kind, fields, body: {"comparisons": {key: {"status": "supported"} for key in fields}},
        "author": _author, "author_provider": "fixture-author", "author_model": "complete-v1", "author_binding": {"binding_sha256": "a" * 64}}
    first = run_checks(database, **options)
    options.pop("occurrence_ids")
    with pytest.raises(ValueError, match="frozen_author_runtime_changed"):
        run_checks(database, **{**options, "author_binding": {"binding_sha256": "b" * 64}, continuation: first["run_id"]})
    with sqlite3.connect(database) as db:
        frozen = json.loads(db.execute("SELECT input_json FROM article_check_runs WHERE run_id=?", (first["run_id"],)).fetchone()[0])
        assert frozen["content_author"]["binding_sha256"] == "a" * 64


def test_scheduled_t1_selects_existing_completed_acquisition_runtime(tmp_path, monkeypatch):
    from test_issue112_acquisition import _batch, _item, _database
    from climate_registry.acquisition import store_acquisition_batch
    from climate_registry.publication import migrate_publication
    from scripts import check_information as cli, run_meeting_extraction
    database = _database(tmp_path)
    migrate_publication(database, tmp_path/"backups", apply=True)
    store_acquisition_batch(database, _batch([_item(body=BODY)]))
    root = tmp_path/"runs"; run = root/"fixture-run"; run.mkdir(parents=True)
    binding = {"registry_database": str(database), "run_id": run.name, "attempt": 1, "created_at": "2026-10-17T00:00:00Z",
        "acquisition_batch_id": "batch-112", "hermes_snapshot": {"sha256": "e" * 64}}
    path = run/"binding.json"; path.write_text(json.dumps(binding))
    monkeypatch.setenv("CLIMATE_ACQUISITION_RUN_DIR", str(root))
    monkeypatch.setattr(cli, "_runtime_identity", lambda *args: {"provider": "real-frozen-provider", "model": "real-frozen-model", "source": "climate-acquisition-fixture-run"})
    callback = lambda request: None
    monkeypatch.setattr(run_meeting_extraction, "_extractor", lambda *args, **kwargs: callback)
    received = {}
    def checks(database, **kwargs):
        received.update(kwargs)
        return {"status": "complete", "item_count": 0, "completed_count": 0}
    monkeypatch.setattr(cli, "run_checks", checks)
    result = tmp_path/"result.json"
    assert cli.main(["--kind", "articles", "--database", str(database), "--backup-dir", str(tmp_path/"backups"),
        "--result", str(result), "--scheduled"], _admitted_occurrence="2026-10-17T00:00:00Z") == 0
    assert received["author"] is callback and received["author_binding"]["binding_path"] == str(path)
    assert received["author_provider"] == "real-frozen-provider"


def test_scheduled_t1_missing_runtime_is_an_explicit_hold_without_deterministic_fallback(tmp_path, monkeypatch):
    from scripts import check_information as cli
    database, _ = _setup(tmp_path)
    monkeypatch.delenv("CLIMATE_ACQUISITION_RUN_DIR", raising=False)
    monkeypatch.setattr(cli, "run_checks", lambda *args, **kwargs: pytest.fail("no silent deterministic scheduled fallback"))
    result = tmp_path/"result.json"
    assert cli.main(["--kind", "articles", "--database", str(database), "--backup-dir", str(tmp_path/"backups"),
        "--result", str(result), "--scheduled"], _admitted_occurrence="2026-10-17T00:00:00Z") == 2
    actual = json.loads(result.read_text())
    assert actual["status"] == "not_configured"
    assert actual["content_enrichment"] == [{"status": "held", "reason": "g7_acquisition_runtime_directory_not_configured"}]


def test_changed_runtime_binding_holds_before_inference(tmp_path):
    from scripts.run_meeting_extraction import _extractor
    from climate_registry.acquisition_review import digest
    frozen = {"run_id": "old-runtime", "hermes_snapshot": {}}
    path = tmp_path/"binding.json"; path.write_text(json.dumps({**frozen, "run_id": "different-runtime"}))
    author = _extractor("fixture", "fixture", path, purpose="content-enrichment", acquisition_sha256=digest(frozen))
    with pytest.raises(ValueError, match="runtime changed"):
        author({})


@pytest.mark.parametrize("wrong_name", ["Coastal Insurance Forum 2027", "World Climate Summit 2028"])
def test_dates_author_cannot_replace_exact_target_occurrence(tmp_path, wrong_name):
    from test_issue136_meetings import _database, _candidate
    from climate_registry.publication import migrate_publication
    from climate_monitor.meetings import process_batch
    a = _candidate(location=None, organizer=None, online_url=None, raw_time_text=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None)
    b = {**a, "name": wrong_name}
    body = a["name"] + ". June 10–12, 2027. " + wrong_name + ". June 10–12, 2027. Climate risk agenda."
    database = _database(tmp_path, [body])
    migrate_publication(database, tmp_path/"backups", apply=True)
    process_batch(database, "batch", prompt_text="fixture", prompt_version="v1", provider="fixture", model="fixture", extractor=lambda request: {"events": [a]})
    result = run_checks(database, kind="meetings", backup_dir=tmp_path/"backups", reverify_all=True,
        fetcher=lambda identity,url: {"article_id": identity, "requested_url": url, "final_url": url, "status": "ok", "content": body, "content_ref": None, "content_hash": sha(body), "failure_reason": None},
        verifier=lambda kind,fields,text: {"comparisons": {key: {"status": "supported"} for key in fields}, "website_candidate": a},
        author=lambda request: {"request_sha256": request["request_sha256"], "entity_type": "Dates", "events": [b]}, author_provider="fixture", author_model="fixture")
    assert result["status"] == "partial" and result["enrichment_failed_count"] == 1, result
    assert "occurrence_identity" in result["content_enrichment"][0]["reason"]
    with sqlite3.connect(database) as db:
        packet = json.loads(db.execute("SELECT packet_json FROM meeting_check_attempts WHERE run_id=?", (result["run_id"],)).fetchone()[0])
        assert packet["website_candidate"] is None and packet["verification_status"] != "verified"
        assert {row[0] for row in db.execute("SELECT name FROM climate_events")} == {a["name"]}
        assert not db.execute("SELECT 1 FROM registry_publication WHERE published_candidate_sha256 IS NOT NULL").fetchone()


def test_legacy_published_taxonomy_change_holds_without_claiming_completion(tmp_path, monkeypatch):
    import climate_monitor.taxonomy as taxonomy
    from climate_registry.information_checks import _generation_complete
    database, approved = _published_a(tmp_path)
    before = _reader(database, tmp_path).article("good")
    original = taxonomy.load_article_taxonomy()
    changed = taxonomy._parse_article_taxonomy_bytes(original.definition_yaml.replace("label: Physical Risk", "label: Catastrophe Exposure").encode())
    monkeypatch.setattr(taxonomy, "load_article_taxonomy", lambda *args: changed)
    with sqlite3.connect(database) as db:
        generated = _generated(db, _target(database), lambda request: pytest.fail("legacy complete summary lacks reuse evidence"))
        assert generated["status"] == "held", generated
        assert "legacy_published_editorial_baseline" in generated["reason"]
        assert not _generation_complete(db, {"content_enrichment": generated}, require_author=True)
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == approved
    assert _reader(database, tmp_path).article("good") == before


def test_template_noop_full_t1_lifecycle_terminates_without_review_or_public_change(tmp_path, monkeypatch):
    from climate_registry.publication import information_ready, prepare_review
    from climate_registry.item_lifecycle import partition_items
    database, approved = _published_a(tmp_path)
    before = _reader(database, tmp_path).article("good")
    with sqlite3.connect(database) as db:
        old = prepare(db, _target(database))[1]["article_body"]
        body = old + "\n\nNavigation: subscribe to alerts."
        _insert_body(db, "body-b", body, "2026-10-16T00:00:00Z")
    calls = []
    def author(request):
        calls.append(request)
        return {"request_sha256": request["request_sha256"], "entity_type": "Article", "substantive_change": False,
            "change_items": [], "no_change_reason": "Only navigation/template wording changed.", "no_change_evidence": "Navigation: subscribe to alerts."}
    options = dict(kind="articles", backup_dir=tmp_path/"backups", occurrence_ids={"core:good"},
        fetcher=lambda identity,url: {"article_id": identity, "requested_url": url, "final_url": url, "status": "ok", "content": body, "content_ref": None, "content_hash": sha(body), "failure_reason": None},
        verifier=lambda kind,fields,text: {"comparisons": {key: {"status": "supported"} for key in fields}}, author=author, author_provider="fixture", author_model="fixture")
    result = run_checks(database, **options, reverify_all=True)
    with sqlite3.connect(database) as db:
        latest = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0]
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (latest,)).fetchone()[0])
        assert information_ready(db, snapshot)
        assert result["status"] == "complete" and result["content_enrichment"][0]["status"] == "no-op", result
        from climate_registry.information_checks import _generation_complete
        from climate_registry.content_enrichment import no_change_snapshot
        packet = json.loads(db.execute("SELECT packet_json FROM article_check_attempts WHERE run_id=?", (result["run_id"],)).fetchone()[0])
        assert _generation_complete(db, packet, require_author=True)
        for broken in ("no_change_evidence", "request_sha256"):
            bad = deepcopy(packet)
            bad["content_enrichment"]["record"]["output"][broken] = "unbound"
            assert not _generation_complete(db, bad, require_author=True)
        bad = deepcopy(snapshot)
        for row in bad["tables"]["article_versions"]:
            row["observed_title"] = "Actually changed title"
        assert not no_change_snapshot(db, bad)
        assert not snapshot.get("derived_display", {}).get("content_update")
        partition = partition_items(db, tmp_path/"lifecycle", [latest])
        assert partition["ready"][0]["stage"] == "no_change", partition
        assert partition["ready"][0]["next_owner"] is None
    for _ in range(2):
        following = run_checks(database, **options)
        assert following["item_count"] == 0 and len(calls) == 1, following
    packet = prepare_review(database, tmp_path/"review")
    assert not any(item["entity_id"] == "good" for item in packet["candidates"])
    assert _reader(database, tmp_path).article("good") == before


def test_legacy_approved_complete_bundle_reuses_summary_and_note_for_editorial_only(tmp_path, monkeypatch):
    import climate_monitor.taxonomy as taxonomy
    from test_enrichment_contract import _stage_update, _native_contract_review
    database, _ = _published_a(tmp_path)
    old_sha, old = _stage_update(database)
    assert "content_authoring" not in old
    _native_contract_review(database, tmp_path, monkeypatch)
    before = _reader(database, tmp_path).article("good")
    original = taxonomy.load_article_taxonomy()
    changed = taxonomy._parse_article_taxonomy_bytes(original.definition_yaml.replace("label: Physical Risk", "label: Catastrophe Exposure").encode())
    monkeypatch.setattr(taxonomy, "load_article_taxonomy", lambda *args: changed)
    calls = []
    def author(request):
        calls.append(request)
        assert request["requested_outputs"] == ["editorial"]
        result = _author(request)
        result["editorial"]["categories"] = ["Catastrophe Exposure"]
        result["editorial"]["evidence"]["categories"][0]["value"] = "Catastrophe Exposure"
        return result
    with sqlite3.connect(database) as db:
        generated = _generated(db, _target(database), author)
        assert generated["status"] == "generated" and len(calls) == 1, generated
        staged = stage(db, _target(database), generated, generated_at="2026-10-20T00:00:00Z")
        assert staged["status"] == "pending_review", staged
        current = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (staged["candidate_sha256"],)).fetchone()[0])
        assert current["derived_display"]["summary"] == old["derived_display"]["summary"]
        assert current["derived_display"]["content_update"] == old["derived_display"]["content_update"]
        assert current["derived_display"]["categories"] == ["Catastrophe Exposure"]
        validate_content_baselines(db, current, staged["candidate_sha256"])
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == old_sha
    assert _reader(database, tmp_path).article("good") == before
