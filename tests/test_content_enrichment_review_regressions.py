"""Real writer regressions for G7 source comparison, prior summaries, dependencies and resume."""
from pathlib import Path
import json
import sqlite3
import pytest
from test_content_enrichment import _setup, _target, _generated, _author
from test_enrichment_contract import _native_contract_review, _insert_body, ordered_source_check_fixture_clock
from climate_registry.content_enrichment import prepare, stage
from climate_registry.article_dates import _body_text

def test_substantive_updated_line_is_not_a_date_only_noop(tmp_path, monkeypatch):
    database, _ = _setup(tmp_path)
    with sqlite3.connect(database) as db:
        assert stage(db, _target(database), _generated(db, _target(database)), generated_at="2026-10-17T00:00:00Z")["status"] == "pending_review"
    _native_contract_review(database, tmp_path, monkeypatch)
    calls = []
    with sqlite3.connect(database) as db:
        old = prepare(db, _target(database))[1]["article_body"]
        body = old + "\n\nUpdated: 22 October 2026 - Flood losses were revised from 20 billion to 40 billion."
        _insert_body(db, "body-substantive-update", body, "2026-10-22T00:00:00Z")
        def author(request):
            calls.append(request)
            output = _author(request)
            output["change_items"] = [{"note": "Flood losses were revised from 20 billion to 40 billion.", "basis": "body_comparison",
                "previous_excerpt": "Flood losses reached 20 billion.", "current_excerpt": request["sections"][-1]}]
            return output
        result = _generated(db, _target(database), author)
        accepted = stage(db, _target(database), result, generated_at="2026-10-22T00:00:01Z")
    print(json.dumps({"normalized_bodies_equal": _body_text(old) == _body_text(body), "author_calls": len(calls), "result": {k:result.get(k) for k in ("status","reason")}, "stage_status":accepted["status"]}, ensure_ascii=False))
    assert _body_text(old) != _body_text(body)
    assert len(calls) == 1 and result["status"] == "generated", result
    assert accepted["status"] == "pending_review"
    assert "40 billion" in result["record"]["output"]["summary"]
    from climate_registry.content_enrichment import validate_noop
    with sqlite3.connect(database) as db:
        _, current = prepare(db, _target(database))
        with pytest.raises(ValueError, match="literal_evidence"):
            validate_noop(db, _target(database), {"status": "no-op", "reason": "unchanged_substantive_body",
                "taxonomy_sha256": current["taxonomy"]["sha256"], "request": current})

@pytest.mark.parametrize("continuation", ["--resume", "--retry"])
def test_manual_g7_resume_restores_its_frozen_author(tmp_path, monkeypatch, continuation):
    from scripts import check_information as cli, run_meeting_extraction
    database, _ = _setup(tmp_path)
    binding = {"registry_database": str(database), "hermes_snapshot": {"sha256": "f" * 64}, "run_id": "fixture-run"}
    path = tmp_path/"binding.json"
    path.write_text(json.dumps(binding))
    monkeypatch.setattr(cli, "_runtime_identity", lambda *args: {"provider": "fixture-author", "model": "complete-v1", "source": "climate-acquisition-fixture-run"})
    monkeypatch.setattr(run_meeting_extraction, "_extractor", lambda *args, **kwargs: _author)
    import climate_registry.information_checks as checks
    from test_content_enrichment import BODY, sha
    monkeypatch.setattr(checks, "fetch_article_content", lambda identity,url,**kwargs: {"article_id": identity, "requested_url": url, "final_url": url, "status": "ok", "content": BODY, "content_ref": None, "content_hash": sha(BODY), "failure_reason": None})
    original = cli.run_checks
    def configured(database, **kwargs):
        return original(database, **kwargs, verifier=lambda kind,fields,body: {"comparisons": {key: {"status":"supported"} for key in fields}})
    monkeypatch.setattr(cli, "run_checks", configured)
    result = tmp_path/"result.json"
    common = ["--kind", "articles", "--database", str(database), "--backup-dir", str(tmp_path/"backups"), "--result", str(result)]
    assert cli.main(common + ["--authoring-binding", str(path), "--occurrence-id", "core:good"]) == 0
    run_id = json.loads(result.read_text())["run_id"]
    assert cli.main(common + [continuation, run_id]) == 0
    path.unlink()
    assert cli.main(common + [continuation, run_id]) == 2
    held = json.loads(result.read_text())
    assert held["blocked_run_id"] == run_id and held["content_enrichment"][0]["status"] == "held"

def test_template_noop_does_not_strand_dates_on_an_unreviewable_article(tmp_path, monkeypatch):
    from test_issue112_acquisition import _database
    from test_issue136_meetings import _stored_batch, _stored_item, _candidate
    from test_registry_publication import _approve_native_packet, _complete_fixture_information
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    from climate_monitor.meetings import process_batch
    from climate_registry.information_checks import run_checks, check_targets
    from climate_registry.publication import stage_entities, prepare_review
    from climate_registry.item_lifecycle import partition_items, _article_dependency, _load
    from climate_registry.content_enrichment import no_change_snapshot, sha
    database = _database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    url = "https://example.com/template-only"
    body_a = ("World Climate Summit 2027 meets June 10–12, 2027 in New York. Registration deadline May 1, 2027. "
              "Register https://example.com/register. Climate risk agenda. The conference is scheduled.")
    article, body_b = None, None
    for label, observed, body in (("a", "2026-09-10T08:00:00Z", body_a), ("b", "2026-10-01T08:00:00Z", body_a + "\n\nNavigation: subscribe to alerts.")):
        payload = _stored_batch("review-body-" + label, observed[:10], observed, _stored_item(url, body, observed))
        store_acquisition_batch(database, payload)
        item = load_acquisition_batch(database, payload["batch_id"])["items"][0]
        with sqlite3.connect(database) as db:
            db.execute("UPDATE articles SET current_content_version_id=? WHERE article_id=?", (item["content_version_id"], item["article_id"]))
        assert process_batch(database, payload["batch_id"], prompt_text="fixture exact event " + label, prompt_version=label,
            provider="fixture", model="fixture", extractor=lambda request: {"events": [_candidate()]})["status"] == "succeeded"
        if label == "a":
            def initial_author(request):
                text, sections = request["article_body"], request["sections"]
                categories, keywords = ["Climate Risk"], ["climate risk agenda", "World Climate Summit", "registration deadline"]
                editorial = {"categories":categories, "keywords":keywords,
                    "evidence":{field:[{"value":value, "excerpt":text, "record_context":text,
                        "reason":"The exact source states the climate risk summit agenda and registration deadline."} for value in values]
                        for field,values in (("categories",categories),("keywords",keywords))}}
                return {"request_sha256":request["request_sha256"], "entity_type":"Article", "summary":" ".join(sections),
                    "summary_support":"complete", "summary_items":[{"statement":text,"excerpt":text} for text in sections],
                    "coverage":[{"section":i,"summary_items":[i]} for i in range(len(sections))], "editorial":editorial,
                    "substantive_change":False, "change_items":[], "date_evidence":{}, "language":"en"}
            initial_target = next(t for t in check_targets(database,"articles") if t["entity_id"] == item["article_id"])
            with sqlite3.connect(database) as db:
                staged = stage(db, initial_target, _generated(db, initial_target, initial_author), generated_at=observed)
                assert staged["status"] == "pending_review", staged
            _complete_fixture_information(database)
            _native_contract_review(database, tmp_path, monkeypatch, identity=item["article_id"])
        else:
            article, body_b = item["article_id"], item["content_version_id"]
    target = next(t for t in check_targets(database, "articles") if t["entity_kind"] == "article" and t["entity_id"] == article)
    body = body_a + "\n\nNavigation: subscribe to alerts."
    def author(request):
        return {"request_sha256": request["request_sha256"], "entity_type": "Article", "substantive_change": False,
                "change_items": [], "no_change_reason": "Only navigation/template wording changed.", "no_change_evidence": "Navigation: subscribe to alerts."}
    result = run_checks(database, kind="articles", backup_dir=tmp_path/"backups", occurrence_ids={target["occurrence_id"]}, reverify_all=True,
        fetcher=lambda identity,url: {"article_id": identity, "requested_url":url, "final_url":url, "status":"ok","content":body,"content_ref":None,"content_hash":sha(body),"failure_reason":None},
        verifier=lambda kind,fields,text: {"comparisons":{key:{"status":"supported"} for key in fields}},
        author=author, author_provider="fixture", author_model="fixture")
    print(json.dumps({"t1_status":result["status"], "enrichment_status":result["content_enrichment"][0]["status"]}))
    assert result["status"] == "complete" and result["content_enrichment"][0]["status"] == "no-op", result
    _complete_fixture_information(database)
    with sqlite3.connect(database) as db:
        shas = dict(db.execute("SELECT entity_id,latest_candidate_sha256 FROM registry_publication"))
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (shas[article],)).fetchone()[0])
        partition = partition_items(db, tmp_path/"lifecycle", list(shas.values()))
        dependency = _article_dependency(db, _load(tmp_path/"lifecycle", database), article, body_b)
        print(json.dumps({"noop_snapshot": no_change_snapshot(db, snapshot), "partition": {key:[{field:item.get(field) for field in ("entity_kind","stage","next_owner","reason")} for item in values] for key,values in partition.items()}, "dependency_requires_approval":dependency["requires_approval"]}))
    assert any(row["entity_id"] == article and row["stage"] == "no_change" for row in partition["ready"]), partition
    packet = prepare_review(database, tmp_path/"lifecycle/review")
    print(json.dumps({"review_candidates": [{"entity_id": item["entity_id"], "entity_kind": item["entity_kind"]} for item in packet["candidates"]]}))
    assert not dependency["requires_approval"]
    assert any(row["entity_kind"] == "meeting" and row["next_owner"] == "G10" for row in partition["ready"]), partition
    assert any(item["entity_kind"] == "meeting" for item in packet["candidates"])
    assert not any(item["entity_id"] == article for item in packet["candidates"])
    assert not any(row["entity_kind"] == "meeting" and row.get("stage") == "dependency_held" for row in partition["held"])
    from climate_registry.item_lifecycle import record_dispute
    with sqlite3.connect(database) as db:
        before_public = list(db.execute("SELECT entity_kind,entity_id,published_candidate_sha256 FROM registry_publication ORDER BY entity_kind,entity_id"))
        latest = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_id=?", (article,)).fetchone()[0]
    record_dispute(tmp_path/"lifecycle", database, latest, text="Check the exact template interpretation.",
        evidence={"literal": "Navigation: subscribe to alerts."}, options=["no_change", "substantive"], impact="Dates dependency")
    with sqlite3.connect(database) as db:
        disputed = _article_dependency(db, _load(tmp_path/"lifecycle", database), article, body_b)
        assert disputed["requires_approval"]
        blocked = partition_items(db, tmp_path/"lifecycle", list(shas.values()))
        assert any(item["entity_kind"] == "meeting" and item["stage"] == "dependency_held" for item in blocked["held"]), blocked
        assert list(db.execute("SELECT entity_kind,entity_id,published_candidate_sha256 FROM registry_publication ORDER BY entity_kind,entity_id")) == before_public
    from climate_registry.capture import _insert_failed_fetch, FetchFailure
    with sqlite3.connect(database) as db:
        _insert_failed_fetch(db, article_id=article, requested_url=url, fetched_at="2026-10-24T00:00:00Z",
            failure=FetchFailure("blocked_response", "No complete current body", http_status=403), id_factory=lambda kind: "after-noop-source-failure")
        assert not no_change_snapshot(db, snapshot)

@pytest.mark.parametrize("bind_body", [False, True])
def test_acquisition_public_summary_is_a_valid_prior_baseline(tmp_path, monkeypatch, bind_body):
    from test_issue112_acquisition import _database
    from test_issue136_meetings import _stored_batch, _stored_item
    from test_registry_publication import _approve_native_packet
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    from climate_registry.information_checks import check_targets
    from climate_registry.content_enrichment import generate, published_summary
    from climate_registry.enrichment_contract import _candidate
    from climate_registry.read_api import RegistryReader
    database = _database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    url = "https://example.com/native-baseline"
    article = None
    for label, observed, body in (("a", "2026-09-10T08:00:00Z", "Flood losses reached 20 billion."),
                                 ("b", "2026-10-01T08:00:00Z", "Flood losses reached 40 billion.")):
        source_item = _stored_item(url, body, observed)
        source_item.update(summary=body, title="Flood loss estimates")
        payload = _stored_batch("native-baseline-" + label, observed[:10], observed, source_item)
        store_acquisition_batch(database, payload)
        item = load_acquisition_batch(database, payload["batch_id"])["items"][0]
        article = item["article_id"]
        if bind_body:
            with sqlite3.connect(database) as db:
                db.execute("UPDATE articles SET current_content_version_id=? WHERE article_id=?", (item["content_version_id"], article))
        if label == "a":
            _approve_native_packet(database, tmp_path/"native-review", tmp_path/"native-hermes.sqlite", "cron_native_baseline")
    reader = RegistryReader(database, repository_root=tmp_path/"application")
    public_summary = reader.article(article)["summary"]
    assert public_summary == "Flood losses reached 20 billion."
    target = next(t for t in check_targets(database,"articles") if t["entity_id"] == article)
    calls = []
    with sqlite3.connect(database) as db:
        prior = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id=?", (article,)).fetchone()[0]
        baseline_encoded = db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (prior,)).fetchone()[0]
        baseline = _candidate(db, prior)
        from climate_registry.read_api import select_snapshot_article_content
        from climate_registry.enrichment_contract import _body as body_reference, validate_content_baselines
        raw_version = baseline["tables"]["articles"][0]["current_content_version_id"]
        if not bind_body:
            assert raw_version is None
            assert db.execute("SELECT current_content_version_id FROM articles WHERE article_id=?", (article,)).fetchone()[0] is None
        selected, retained = select_snapshot_article_content(baseline, article, raw_version)
        assert retained["markdown_content"] == "Flood losses reached 20 billion."
        _, request = prepare(db, target)
        assert request["previous"]["candidate_sha256"] == prior
        assert request["previous"]["content_version_id"] == selected["content_version_id"]
        assert request["previous"]["article_body"] == retained["markdown_content"]
        if not bind_body:
            from copy import deepcopy
            unavailable = deepcopy(baseline)
            for fetch in unavailable["tables"]["article_fetches"]:
                fetch["fetch_status"] = "failed"
            assert body_reference(unavailable) is None
            broken_pointer = deepcopy(baseline)
            broken_pointer["tables"]["articles"][0]["current_content_version_id"] = "unretained-invalid-version"
            assert select_snapshot_article_content(broken_pointer, article, "unretained-invalid-version")[1] is not None
            assert body_reference(broken_pointer) is None
            foreign_body = deepcopy(baseline)
            for row in foreign_body["tables"]["article_content_versions"]:
                row["article_id"] = "another-article"
            assert body_reference(foreign_body) is None
        def author(request):
            calls.append(request)
            output = _author(request)
            output["change_items"] = [{"note": "Flood losses increased from 20 to 40 billion.", "basis": "body_comparison",
                "previous_excerpt": "Flood losses reached 20 billion.", "current_excerpt": "Flood losses reached 40 billion."}]
            return output
        result = generate(db, target, author, provider="fixture", model="fixture")
        print(json.dumps({"public_summary": public_summary, "g7_prior_summary": published_summary(baseline), "article_versions":len(baseline["tables"]["article_versions"]), "acquisition_summaries":[row["summary"] for row in baseline["tables"]["acquisition_items"]], "author_calls":len(calls), "result":result}))
        assert published_summary(baseline) == public_summary
        assert result["status"] == "generated" and len(calls) == 1, result
        assert result["record"]["request"]["previous"]["summary"] == public_summary
        assert result["record"]["request"]["previous"]["candidate_sha256"] == prior
        accepted = stage(db, target, result, generated_at="2026-10-02T00:00:00Z")
        assert accepted["status"] == "pending_review", accepted
        candidate = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (accepted["candidate_sha256"],)).fetchone()[0])
        bundle = candidate["derived_display"]["content_update"]
        assert bundle["schema_version"] == "article-content-update.v1"
        assert bundle["previous_content_version_id"] == selected["content_version_id"]
        assert bundle["summary"] == "Flood losses reached 40 billion."
        assert bundle["change_evidence"]["items"][0]["previous_excerpt"] == "Flood losses reached 20 billion."
        validate_content_baselines(db, candidate, accepted["candidate_sha256"])
        assert candidate["enrichment_scope"]["content_comparison_baseline"]["candidate_sha256"] == prior
        assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (prior,)).fetchone()[0] == baseline_encoded
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id=?", (article,)).fetchone()[0] == prior
        assert published_summary(baseline) == "Flood losses reached 20 billion."
    from climate_registry.information_checks import run_checks
    from climate_registry.content_enrichment import sha
    outcome = run_checks(database, kind="articles", backup_dir=tmp_path/"backups", occurrence_ids={target["occurrence_id"]}, reverify_all=True,
        fetcher=lambda identity,url: {"article_id":identity, "requested_url":url, "final_url":url, "status":"ok", "content":"Flood losses reached 40 billion.",
            "content_ref":None, "content_hash":sha("Flood losses reached 40 billion."), "failure_reason":None},
        verifier=lambda kind,fields,body: {"comparisons":{key:{"status":"supported"} for key in fields}}, author=author, author_provider="fixture", author_model="fixture")
    assert outcome["status"] == "complete" and outcome["enrichment_failed_count"] == 0, outcome
    assert outcome["content_enrichment"][0]["status"] == "pending_review"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (prior,)).fetchone()[0] == baseline_encoded
        if not bind_body:
            assert db.execute("SELECT current_content_version_id FROM articles WHERE article_id=?", (article,)).fetchone()[0] is None
    assert reader.article(article)["summary"] == public_summary
    assert reader.article(article)["available_content"]["content_version_id"] == selected["content_version_id"]





@pytest.mark.parametrize("continuation", ["--resume", "--retry"])
def test_manual_legacy_resume_keeps_no_author_even_when_ambient_runtime_exists(tmp_path, monkeypatch, continuation):
    from scripts import check_information as cli
    import climate_registry.information_checks as checks
    from test_content_enrichment import BODY, sha
    database, _ = _setup(tmp_path)
    monkeypatch.setenv("CLIMATE_ACQUISITION_RUN_DIR", str(tmp_path/"ambient-new-runtime"))
    monkeypatch.setattr(cli, "_latest_authoring_binding", lambda *args: pytest.fail("legacy continuation cannot choose ambient runtime"))
    monkeypatch.setattr(checks, "fetch_article_content", lambda identity,url,**kwargs: {"article_id": identity, "requested_url": url,
        "final_url": url, "status": "ok", "content": BODY, "content_ref": None, "content_hash": sha(BODY), "failure_reason": None})
    original = cli.run_checks
    monkeypatch.setattr(cli, "run_checks", lambda database,**kwargs: original(database, **kwargs,
        verifier=lambda kind,fields,body: {"comparisons": {key: {"status": "supported"} for key in fields}}))
    result = tmp_path/"result.json"
    common = ["--kind", "articles", "--database", str(database), "--backup-dir", str(tmp_path/"backups"), "--result", str(result)]
    assert cli.main(common + ["--occurrence-id", "core:good"]) == 0
    run_id = json.loads(result.read_text())["run_id"]
    with sqlite3.connect(database) as db:
        frozen = json.loads(db.execute("SELECT input_json FROM article_check_runs WHERE run_id=?", (run_id,)).fetchone()[0])
        assert not frozen.get("content_author")
    assert cli.main(common + [continuation, run_id]) == 0


@pytest.mark.parametrize("label", ["Updated", "Last updated", "Published", "Revision date"])
@pytest.mark.parametrize("value", ["22 October 2026", "October 2026", "2026-10-22"])
def test_date_value_normalization_preserves_same_line_material_words(label, value):
    base = "Flood losses reached 20 billion."
    assert _body_text(base + " " + label + ": " + value) == _body_text(base)
    assert _body_text(base + " " + label + ": " + value + " - Losses are now 40 billion.") != _body_text(base)
    assert "40 billion" in _body_text(base + " " + label + ": " + value + " - Losses are now 40 billion.")


def test_unrecognized_updated_value_is_never_removed_as_a_date():
    for value in ("Loss estimates revised to 40 billion.", "2026-99-31 - 40 billion", "unknown - 40 billion"):
        assert "40 billion" in _body_text("Updated: " + value)


@pytest.mark.parametrize("old_fact,new_fact", [
    ("Projected insured loss change is -20 percent.", "Projected insured loss change is +20 percent."),
    ("The warming threshold is <2 degrees Celsius.", "The warming threshold is >2 degrees Celsius."),
    ("The warming threshold is .5 degrees Celsius.", "The warming threshold is 5 degrees Celsius."),
    ("Projected insured loss change is -**20 percent**.", "Projected insured loss change is +**20 percent**."),
    ("The warming threshold is .**5** degrees Celsius.", "The warming threshold is **5** degrees Celsius."),
    ("Projected loss change is -**`20`**bps.", "Projected loss change is +**`20`**bps."),
    ("Projected loss change is -**_20_**bps.", "Projected loss change is +**_20_**bps."),
    ("Warming threshold is .**`5`**degrees.", "Warming threshold is **`5`**degrees."),
    ("Projected loss is $20 billion.", "Projected loss is €20 billion."),
    ("Projected loss rate is 0.2%.", "Projected loss rate is 0.2."),
    ("Projected loss change is -US$20 billion.", "Projected loss change is +US$20 billion."),
    ("Loss change is -<span>USD 20</span>.", "Loss change is +<span>USD 20</span>."),
    ("Loss change is -USD 20.", "Loss change is USD 20."),
    ("-<span>$20</span> billion projected loss.", "+<span>$20</span> billion projected loss."),
    (".<strong>5</strong> degrees warming threshold.", "<strong>5</strong> degrees warming threshold."),
    ("- <span>$20</span> billion projected loss.", "+ <span>$20</span> billion projected loss."),
])
def test_numeric_sign_change_generates_complete_pending_update(tmp_path, monkeypatch, old_fact, new_fact):
    from climate_registry.content_enrichment import generate, sha, validate_noop
    from climate_registry.enrichment_contract import validate_content_baselines
    from climate_registry.information_checks import run_checks
    from test_verified_publication import _reader
    database, _ = _setup(tmp_path)
    def author(request):
        response = _author(request)
        if request.get("previous"):
            response["change_items"] = [{"note": "The source revises the projected change or warming threshold.", "basis": "body_comparison",
                "previous_excerpt": request["previous"]["article_body"], "current_excerpt": request["article_body"]}]
        return response
    with sqlite3.connect(database) as db:
        body = old_fact + " Coastal insurance remains exposed.\n\nAdaptation reduced flood claims by 30 percent."
        _insert_body(db, "numeric-body-a", body, "2026-10-20T00:00:00Z")
        generated = _generated(db, _target(database), author)
        assert generated["status"] == "generated", generated
        assert stage(db, _target(database), generated, generated_at="2026-10-20T00:00:01Z")["status"] == "pending_review"
    _native_contract_review(database, tmp_path, monkeypatch)
    before_public = _reader(database, tmp_path).article("good")
    calls = []
    def current_author(request):
        calls.append(request)
        return author(request)
    with sqlite3.connect(database) as db:
        if "$" in old_fact or "%" in old_fact:
            _insert_body(db, "numeric-body-presentation", body.replace(old_fact, "**" + old_fact + "**"), "2026-10-20T12:00:00Z")
            presentation = generate(db, _target(database), current_author, provider="fixture-author", model="complete-v1")
            assert presentation["status"] == "no-op" and not calls, presentation
            validate_noop(db, _target(database), presentation)
        baseline_sha = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0]
        baseline_json = db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (baseline_sha,)).fetchone()[0]
        raw_before = db.execute("SELECT * FROM article_content_versions ORDER BY content_version_id").fetchall()
        fetch_before = db.execute("SELECT * FROM article_fetches ORDER BY fetch_id").fetchall()
        new_body = body.replace(old_fact, new_fact)
        _insert_body(db, "numeric-body-b", new_body, "2026-10-21T00:00:00Z")
        target = _target(database)
        generated = generate(db, target, current_author, provider="fixture-author", model="complete-v1",
            runtime_binding={"provider":"fixture-author", "model":"complete-v1"})
        assert len(calls) == 1 and generated["status"] == "generated", generated
        assert _body_text(body) != _body_text(new_body)
        assert generated["record"]["output"]["summary"] == " ".join(new_body.split("\n\n"))
        accepted = stage(db, target, generated, generated_at="2026-10-21T00:00:01Z")
        assert accepted["status"] == "pending_review", accepted
        candidate = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (accepted["candidate_sha256"],)).fetchone()[0])
        bundle = candidate["derived_display"]["content_update"]
        assert bundle["schema_version"] == "article-content-update.v1" and new_fact in bundle["summary"]
        assert bundle["previous_content_version_id"] == "numeric-body-a" and bundle["content_version_id"] == "numeric-body-b"
        assert bundle["change_evidence"]["items"][0]["previous_excerpt"] == body
        assert bundle["change_evidence"]["items"][0]["current_excerpt"] == new_body
        assert candidate["enrichment_scope"]["content_comparison_baseline"]["candidate_sha256"] == baseline_sha
        validate_content_baselines(db, candidate, accepted["candidate_sha256"])
        _, current = prepare(db, target)
        with pytest.raises(ValueError, match="literal_evidence"):
            validate_noop(db, target, {"status":"no-op", "request":current, "taxonomy_sha256":current["taxonomy"]["sha256"]})
    result = run_checks(database, kind="articles", backup_dir=tmp_path/"backups", occurrence_ids={target["occurrence_id"]}, reverify_all=True,
        fetcher=lambda identity,url: {"article_id":identity,"requested_url":url,"final_url":url,"status":"ok","content":new_body,
            "content_ref":None,"content_hash":sha(new_body),"failure_reason":None},
        verifier=lambda kind,fields,body: {"comparisons":{key:{"status":"supported"} for key in fields}},
        author=current_author, author_provider="fixture-author", author_model="complete-v1")
    assert result["status"] == "complete" and result["content_enrichment"][0]["status"] == "pending_review", result
    assert len(calls) == 1
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == baseline_sha
        assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (baseline_sha,)).fetchone()[0] == baseline_json
        assert set(raw_before).issubset(db.execute("SELECT * FROM article_content_versions").fetchall())
        assert set(fetch_before).issubset(db.execute("SELECT * FROM article_fetches").fetchall())
    assert _reader(database, tmp_path).article("good") == before_public


@pytest.mark.parametrize("fact", ["Loss change is -20 percent.", "Warming stays <2 degrees Celsius.", "Warming stays .5 degrees Celsius."])
def test_numeric_sign_comparison_ignores_display_only_changes(fact):
    import html
    assert _body_text(fact) == _body_text("<p><strong>" + html.escape(fact) + "</strong></p>")
    assert _body_text(fact) == _body_text("**" + fact + "**")
    assert _body_text(fact) == _body_text("> " + fact)
    assert _body_text(fact) == _body_text(">>" + fact)
    assert _body_text(fact) == _body_text(fact + "\n> ")
    assert _body_text(fact + " https://example.org/source") == _body_text(fact + " <https://example.org/source>")
    assert _body_text(fact) == _body_text(fact.replace(" ", "   "))
    assert _body_text(fact) == _body_text(fact.replace("-20", "- 20").replace("<2", "< 2"))
    assert _body_text(fact) == _body_text(fact + " Updated: 22 October 2026")


@pytest.mark.parametrize("old,new", [("-.5", "+.5"), ("<=2", "<2"), ("−20", "+20")])
def test_numeric_comparison_keeps_fractional_signed_and_inclusive_values(old, new):
    assert _body_text("Climate projection is " + old) != _body_text("Climate projection is " + new)


@pytest.mark.parametrize("comparison", [">2", ">=2", "<2", "<=2"])
def test_numeric_comparison_at_line_start_is_not_a_markdown_quote(comparison):
    assert _body_text(comparison + " degrees Celsius") != _body_text("2 degrees Celsius")


@pytest.mark.parametrize("marker", ["**", "*", "__", "_", "~~", "`"])
def test_inline_numeric_presentation_keeps_sign_and_decimal_value(marker):
    negative = "Loss change is -" + marker + "20 percent" + marker
    positive = "Loss change is +" + marker + "20 percent" + marker
    fractional = "Threshold is ." + marker + "5" + marker
    integral = "Threshold is " + marker + "5" + marker
    assert _body_text(negative) == _body_text("Loss change is -20 percent")
    assert _body_text(negative) != _body_text(positive)
    assert _body_text(fractional) == _body_text("Threshold is .5")
    assert _body_text(fractional) != _body_text(integral)


def test_inline_code_presentation_keeps_literal_underscore_identifier():
    assert _body_text("`risk_floor`") == _body_text("risk_floor")
    assert _body_text("`risk_floor`") != _body_text("`riskfloor`")


ADJACENT_PRESENTATION_CASES = [
    ("-**20**bps", "+**20**bps", "-20bps", "+20bps"),
    ("**-20**bps", "**+20**bps", "-20bps", "+20bps"),
    ("-*20*degrees", "+*20*degrees", "-20degrees", "+20degrees"),
    ("-~~20~~%", "+~~20~~%", "-20%", "+20%"),
    ("-~~20~~bps", "+~~20~~bps", "-20bps", "+20bps"),
    ("-`20`bps", "+`20`bps", "-20bps", "+20bps"),
    (".**5**degrees", "**5**degrees", ".5degrees", "5degrees"),
    ("`.5`bps", "`5`bps", ".5bps", "5bps"),
    ("<*2*degrees", ">*2*degrees", "<2degrees", ">2degrees"),
    ("**<2**degrees", "**>2**degrees", "<2degrees", ">2degrees"),
    ("-_20_ bps", "+_20_ bps", "-20 bps", "+20 bps"),
    ("-__20__%", "+__20__%", "-20%", "+20%"),
    ("-**20** bps", "+**20** bps", "-20 bps", "+20 bps"),
    ("-**20**°C", "+**20**°C", "-20°C", "+20°C"),
    ("-**`20`**bps", "+**`20`**bps", "-20bps", "+20bps"),
    ("-**_20_**bps", "+**_20_**bps", "-20bps", "+20bps"),
    (".**`5`**degrees", "**`5`**degrees", ".5degrees", "5degrees"),
]


@pytest.mark.parametrize("old,new,plain_old,plain_new", ADJACENT_PRESENTATION_CASES)
def test_supported_adjacent_numeric_presentation(old, new, plain_old, plain_new):
    assert _body_text(old) == _body_text(plain_old)
    assert _body_text(new) == _body_text(plain_new)
    assert _body_text(old) != _body_text(new)


@pytest.mark.parametrize("identifier", ["risk_floor", "risk_floor_more", "_20_bps", "20_bps", "__20__bps"])
def test_underscore_identifiers_and_code_are_literal(identifier):
    assert _body_text(identifier) == identifier.casefold()
    assert _body_text("`" + identifier + "`") == _body_text(identifier)
    assert _body_text(identifier) != _body_text(identifier.replace("_", ""))


@pytest.mark.parametrize("old_fact,new_fact", [
    ("Projected insured loss change is -**20**bps.", "Projected insured loss change is +**20**bps."),
    ("The warming threshold is .**5**degrees.", "The warming threshold is **5**degrees."),
    ("Projected loss change is -**`20`**bps.", "Projected loss change is +**`20`**bps."),
    ("Projected loss change is -**_20_**bps.", "Projected loss change is +**_20_**bps."),
    ("Warming threshold is .**`5`**degrees.", "Warming threshold is **`5`**degrees."),
    ("Projected loss is $20 billion.", "Projected loss is €20 billion."),
    ("Projected loss rate is 0.2%.", "Projected loss rate is 0.2."),
    ("Projected loss change is -US$20 billion.", "Projected loss change is +US$20 billion."),
    ("Loss change is -<span>USD 20</span>.", "Loss change is +<span>USD 20</span>."),
    ("Loss change is -USD 20.", "Loss change is USD 20."),
    ("-<span>$20</span> billion projected loss.", "+<span>$20</span> billion projected loss."),
    (".<strong>5</strong> degrees warming threshold.", "<strong>5</strong> degrees warming threshold."),
    ("- <span>$20</span> billion projected loss.", "+ <span>$20</span> billion projected loss."),
])
def test_adjacent_numeric_bundle_t1_and_noop_share_one_comparison(tmp_path, monkeypatch, old_fact, new_fact):
    from climate_registry.content_enrichment import VERSION, PROMPT, sha, validate_noop
    from climate_registry.acquisition_review import digest
    from climate_registry.enrichment_contract import validate_content_baselines
    from climate_registry.information_checks import run_checks
    from test_verified_publication import _reader
    database, _ = _setup(tmp_path)
    def author(request):
        response = _author(request)
        response["change_items"] = [{"note":"The source revises the projected numeric fact.","basis":"body_comparison",
            "previous_excerpt":request["previous"]["article_body"],"current_excerpt":request["article_body"]}]
        return response
    old_body = old_fact + " Coastal insurance remains exposed.\n\nAdaptation reduced flood claims by 30 percent."
    new_body = old_body.replace(old_fact, new_fact)
    with sqlite3.connect(database) as db:
        _insert_body(db, "adjacent-body-a", old_body, "2026-10-20T00:00:00Z")
        assert stage(db, _target(database), _generated(db, _target(database), author), generated_at="2026-10-20T00:00:01Z")["status"] == "pending_review"
    _native_contract_review(database, tmp_path, monkeypatch)
    before_public = _reader(database, tmp_path).article("good")
    with sqlite3.connect(database) as db:
        baseline = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0]
        baseline_json = db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (baseline,)).fetchone()[0]
        old_rows = db.execute("SELECT * FROM article_content_versions").fetchall()
        old_fetches = db.execute("SELECT * FROM article_fetches").fetchall()
        _insert_body(db, "adjacent-body-b", new_body, "2026-10-21T00:00:00Z")
    target = _target(database)
    with sqlite3.connect(database) as db:
        _, request = prepare(db, target)
        output = author({**request,"request_sha256":digest(request)})
        record = {"schema_version":VERSION,"request":request,"output":output,"provider":"fixture-author", "model":"complete-v1",
            "prompt_sha256":sha(PROMPT),"runtime_binding":None}
        try:
            g1 = stage(db, target, {"status":"generated","record":record}, generated_at="2026-10-21T00:00:01Z")
        except ValueError as exc:
            g1 = {"status":"held","reason":str(exc)}
        try:
            validate_noop(db, target, {"status":"no-op","request":request,"taxonomy_sha256":request["taxonomy"]["sha256"]})
            noop_rejected = False
        except ValueError:
            noop_rejected = True
    calls = []
    def current_author(request):
        calls.append(request)
        return author(request)
    options = dict(kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={target["occurrence_id"]},
        fetcher=lambda identity,url: {"article_id":identity,"requested_url":url,"final_url":url,"status":"ok","content":new_body,
            "content_ref":None,"content_hash":sha(new_body),"failure_reason":None},
        verifier=lambda kind,fields,body: {"comparisons":{key:{"status":"supported"} for key in fields}},
        author=current_author,author_provider="fixture-author",author_model="complete-v1")
    result = run_checks(database, **options, reverify_all=True)
    print(json.dumps({"g1":g1,"t1":result,"author_calls":len(calls),"noop_rejected":noop_rejected}))
    assert g1["status"] == "pending_review", g1
    assert noop_rejected
    assert result["status"] == "complete" and result["content_enrichment"][0]["status"] == "pending_review", result
    assert len(calls) == 1
    repeated = run_checks(database, **options)
    assert repeated["item_count"] == 0 and len(calls) == 1
    with sqlite3.connect(database) as db:
        latest = db.execute("SELECT latest_candidate_sha256,published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()
        assert latest[1] == baseline
        assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (baseline,)).fetchone()[0] == baseline_json
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (latest[0],)).fetchone()[0])
        bundle = snapshot["derived_display"]["content_update"]
        assert new_fact in bundle["summary"] and "Adaptation" in bundle["summary"]
        assert bundle["previous_content_version_id"] == "adjacent-body-a" and bundle["content_version_id"] == "adjacent-body-b"
        assert bundle["change_evidence"]["items"][0]["previous_excerpt"] == old_body
        assert bundle["change_evidence"]["items"][0]["current_excerpt"] == new_body
        validate_content_baselines(db, snapshot, latest[0])
        assert set(old_rows).issubset(db.execute("SELECT * FROM article_content_versions").fetchall())
        assert set(old_fetches).issubset(db.execute("SELECT * FROM article_fetches").fetchall())
    assert _reader(database, tmp_path).article("good") == before_public


@pytest.mark.parametrize("identifier", ["risk_floor", "_20_bps", "__20__bps"])
def test_one_inner_code_layer_keeps_literal_identifier(identifier):
    assert _body_text("**`" + identifier + "`**") == _body_text(identifier)
    assert _body_text("**`" + identifier + "`**") != _body_text(identifier.replace("_", ""))


def test_nested_code_underscores_are_not_reparsed_as_emphasis():
    assert _body_text("**`_20_`**bps") == _body_text("`_20_`bps")
    assert _body_text("**`_20_`**bps") != _body_text("**_20_**bps")


@pytest.mark.parametrize("old,new", [
    ("$20", "€20"), ("20$", "20€"), ("£20", "¥20"), ("₹20", "₩20"),
    ("US$20", "C$20"), ("0.2%", "0.2"), ("0.2‰", "0.2%"),
    ("0.2‱", "0.2‰"), ("0.2٪", "0.2"), ("0.2％", "0.2"),
    ("-$20", "+$20"), ("$-20", "$+20"), ("-US$20", "+US$20"),
    ("−€0.2", "+€0.2"), ("<€0.2", ">€0.2"), ("$.5", "$5"),
    ("20 USD", "20 EUR"), ("20 percent", "20 bps"),
])
def test_currency_and_rate_units_preserve_material_source_facts(old, new):
    assert _body_text(old) != _body_text(new)


def test_currency_symbol_category_and_rate_marks_survive_tokenization():
    for symbol in "$€£¥₹₩₽¢¤%‰‱٪％":
        assert symbol in _body_text(symbol + "20")
        assert _body_text(symbol + "20") != _body_text("20")


@pytest.mark.parametrize("plain,styled", [
    ("$20", "$ 20"), ("20€", "20 €"), ("US$20", "US$ 20"),
    ("0.2%", "0.2 %"), ("0.2%", "0.2&nbsp;%"),
    ("$20", "&dollar;20"), ("€20", "&euro;20"), ("£20", "&pound;20"),
    ("$20", "$**20**"), ("$20", "**$20**"), ("€20", "**`€20`**"),
    ("0.2%", "**0.2**%"), ("0.2%", "0.2**%**"),
    ("0.2%", "**`0.2`**%"), ("0.2%", "0.2<em>%</em>"),
    ("-$20", "-**$20**"), ("-US$20", "-US$**20**"), ("$-20", "$**-20**"),
    ("Change is -$20", "Change is - $20"), ("+US$20", "+ **US$20**"),
])
def test_currency_and_rate_unit_presentation_is_noop(plain, styled):
    assert _body_text(plain) == _body_text(styled)


@pytest.mark.parametrize("plain,styled", [
    ("-$20", "-<span>$20</span>"), ("+$20", "+<span>$20</span>"),
    (".5 degrees", ".<strong>5</strong> degrees"),
    ("-20bps", "-<em>20</em>bps"), (".5%", ".<b>5</b>%"),
    ("20bps", "<i>20</i>bps"), ("20%", "<s>20</s>%"),
    (".5%", ".<del>5</del>%"), (".5%", ".<code>5</code>%"),
    ("risk_floor", "<code>risk_floor</code>"),
    (".5%", ".<strike>5</strike>%"), (".5%", ".<font>5</font>%"),
    (".5%", ".<small>5</small>%"), (".5%", ".<mark>5</mark>%"),
    ("-$20", "-<u>$20</u>"), (".5%", '.<a href="/source">5</a>%'),
    (".5%", ".<abbr>5</abbr>%"), (".5%", ".<time>5</time>%"),
    ("Coastal loss", "<p>Coastal</p><p>loss</p>"),
    ("Coastal loss", "<h2>Coastal</h2>loss"),
    ("Coastal loss", "<li>Coastal</li>loss"),
    ("Coastal loss", "Coastal<br>loss"),
    ("Coastal loss", "Coastal <span>loss</span>"),
])
def test_inline_html_keeps_adjacent_numeric_tokens_and_block_boundaries(plain, styled):
    assert _body_text(plain) == _body_text(styled)


@pytest.mark.parametrize("body", [
    "-<span>$20</span> billion loss.", "+<span>$20</span> billion loss.",
    ".<strong>5</strong> degrees threshold.", "<strong>5</strong> degrees threshold.",
])
def test_acquisition_writer_retains_inline_html_body_exactly(tmp_path, body):
    from test_issue112_acquisition import _batch, _item, _database
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    from climate_registry.content_enrichment import sha
    database = _database(tmp_path)
    store_acquisition_batch(database, _batch([_item(body=body)]))
    item = load_acquisition_batch(database, "batch-112")["items"][0]
    assert item["markdown_content"] == body and item["content_sha256"] == sha(body)


def test_capture_concatenates_inline_numeric_text_before_acquisition():
    from climate_registry.capture import extract_html
    source = b"<p>Loss is -<span>$20</span> and rate is <strong>0.2</strong>%; threshold .<strong>5</strong>.</p>"
    assert extract_html(source, base_url="https://example.org/article", charset="utf-8") == "Loss is -$20 and rate is 0.2%; threshold .5."


@pytest.mark.parametrize("html,expected", [
    (b"<p>- <span>$20</span></p>", "- $20"),
    (b"<ul><li>$20</li></ul>", "- $20"),
    (b"<p>-<span>$20</span></p>", "-$20"),
])
def test_flattened_currency_sign_is_never_assumed_to_be_an_unsigned_bullet(tmp_path, html, expected):
    from climate_registry.capture import extract_html
    text = extract_html(html, base_url="https://example.org/article", charset="utf-8")
    assert text == expected
    assert _body_text(text) != _body_text("$20")
    assert (_body_text(text) == _body_text("-$20")) is (expected == "-$20")
    from test_issue112_acquisition import _batch, _item, _database
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    database = _database(tmp_path)
    store_acquisition_batch(database, _batch([_item(body=text)]))
    assert load_acquisition_batch(database, "batch-112")["items"][0]["markdown_content"] == text


@pytest.mark.parametrize("fact", ["$20", "20%"])
def test_leading_spaced_numeric_minus_keeps_its_writer_ambiguity(fact):
    assert _body_text("- " + fact) != _body_text(fact)
    assert _body_text("- " + fact) != _body_text("-" + fact)
    assert _body_text("Change is - " + fact) == _body_text("Change is -" + fact)
    assert _body_text("- Loss is " + fact) == _body_text("Loss is " + fact)


@pytest.mark.parametrize("amount", ["$20 billion", "20 percent"])
@pytest.mark.parametrize("source", ["negative-paragraph", "flattened-list"])
def test_ambiguous_numeric_boundary_requires_semantic_noop_or_hold(tmp_path, monkeypatch, source, amount):
    from climate_registry.content_enrichment import sha, validate_noop
    from climate_registry.information_checks import run_checks
    from test_verified_publication import _reader
    database, _ = _setup(tmp_path)
    old_body = "-" + amount + " projected loss. Coastal insurance remains exposed.\n\nAdaptation reduced flood claims by 30 percent."
    def initial_author(request):
        output = _author(request)
        output["change_items"] = [{"note":"The source revises the projected loss.","basis":"body_comparison",
            "previous_excerpt":request["previous"]["article_body"],"current_excerpt":request["article_body"]}]
        return output
    with sqlite3.connect(database) as db:
        _insert_body(db, "ambiguous-a", old_body, "2026-10-20T00:00:00Z")
        assert stage(db, _target(database), _generated(db, _target(database), initial_author), generated_at="2026-10-20T00:00:01Z")["status"] == "pending_review"
    _native_contract_review(database, tmp_path, monkeypatch)
    before = _reader(database, tmp_path).article("good")
    new_body = old_body.replace("-" + amount, "<p>- <span>" + amount + "</span></p>" if source == "negative-paragraph" else "- " + amount)
    with sqlite3.connect(database) as db:
        baseline = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0]
        baseline_json = db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (baseline,)).fetchone()[0]
        raw = db.execute("SELECT * FROM article_content_versions").fetchall()
        _insert_body(db, "ambiguous-b", new_body, "2026-10-21T00:00:00Z")
        _, request = prepare(db, _target(database))
        try:
            validate_noop(db, _target(database), {"status":"no-op","request":request,"taxonomy_sha256":request["taxonomy"]["sha256"]})
            bare_noop_rejected = False
        except ValueError:
            bare_noop_rejected = True
    calls = []
    def author(request):
        calls.append(request)
        if source == "flattened-list":
            raise ValueError("ambiguous_leading_numeric_marker_requires_original_source_evidence")
        return {"request_sha256":request["request_sha256"],"entity_type":"Article","substantive_change":False,
            "change_items":[],"no_change_reason":"The literal paragraph retains the negative amount; only inline presentation changed.",
            "no_change_evidence":"<p>- <span>" + amount + "</span></p>"}
    options = dict(kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={"core:good"},
        fetcher=lambda identity,url:{"article_id":identity,"requested_url":url,"final_url":url,"status":"ok","content":new_body,
            "content_ref":None,"content_hash":sha(new_body),"failure_reason":None},
        verifier=lambda kind,fields,body:{"comparisons":{key:{"status":"supported"} for key in fields}},
        author=author,author_provider="fixture-author",author_model="complete-v1")
    result = run_checks(database, **options, reverify_all=True)
    print(json.dumps({"source":source,"result":result,"author_calls":len(calls),"bare_noop_rejected":bare_noop_rejected}))
    assert bare_noop_rejected and len(calls) == 1
    outcome = result["content_enrichment"][0]
    if source == "flattened-list":
        assert result["status"] == "partial" and outcome["status"] == "held", result
        assert "ambiguous_leading_numeric_marker" in outcome["reason"]
    else:
        assert result["status"] == "complete" and outcome["status"] == "no-op", result
        repeated = run_checks(database, **options)
        assert repeated["item_count"] == 0 and len(calls) == 1
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0] == baseline
        assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (baseline,)).fetchone()[0] == baseline_json
        assert set(raw).issubset(db.execute("SELECT * FROM article_content_versions").fetchall())
        if source == "negative-paragraph":
            latest = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0]
            snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (latest,)).fetchone()[0])
            assert not snapshot.get("derived_display",{}).get("content_update")
    assert _reader(database,tmp_path).article("good") == before


@pytest.mark.parametrize("gap", ["\u00a0", "&nbsp;", "&#160;", "\u202f"])
def test_horizontal_currency_marker_matches_actual_capture_spacing(tmp_path, gap):
    from climate_registry.capture import extract_html
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    from test_issue112_acquisition import _batch, _item, _database
    body = "<p>-" + gap + "<span>$20</span></p>"
    assert extract_html(body.encode(), base_url="https://example.org/article", charset="utf-8") == "- $20"
    database = _database(tmp_path)
    store_acquisition_batch(database, _batch([_item(body=body)]))
    assert load_acquisition_batch(database, "batch-112")["items"][0]["markdown_content"] == body
    for amount in ("$20", "20%"):
        assert _body_text("-" + gap + amount) == _body_text("- " + amount)
        assert _body_text("-" + gap + amount) != _body_text("-" + amount)
        assert _body_text("Change is -" + gap + amount) == _body_text("Change is -" + amount)


@pytest.mark.parametrize("initial", [False, True], ids=["published-update", "initial-generation"])
def test_complete_g7_t1_returns_final_ready_handoff_on_first_pass(tmp_path, initial):
    from test_content_enrichment import BODY
    from test_verified_publication import _database, _reader
    from climate_registry.read_api import RegistryNotFoundError
    from climate_registry.content_enrichment import sha, VERSION
    from climate_registry.information_checks import check_targets, run_checks
    from climate_registry.publication import information_ready
    from climate_registry.item_lifecycle import partition_items
    from climate_registry.enrichment_contract import validate_content_baselines
    if initial:
        database, approved = _database(tmp_path), None
        with sqlite3.connect(database) as db:
            _insert_body(db, "initial-body", BODY, "2026-10-16T00:00:00Z")
        before_public = None
    else:
        database, approved = _setup(tmp_path)
        before_public = _reader(database, tmp_path).article("good")
    original_target = check_targets(database,"articles",{"core:good"})[0]
    with sqlite3.connect(database) as db:
        raw = db.execute("SELECT * FROM article_content_versions").fetchall()
        fetches = db.execute("SELECT * FROM article_fetches").fetchall()
        old_snapshot = db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(approved,)).fetchone() if approved else None
    calls = []
    def author(request):
        calls.append(request)
        return _author(request)
    options = dict(kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={"core:good"},
        fetcher=lambda identity,url:{"article_id":identity,"requested_url":url,"final_url":url,"status":"ok","content":BODY,
            "content_ref":None,"content_hash":sha(BODY),"failure_reason":None},
        verifier=lambda kind,fields,body:{"comparisons":{key:{"status":"supported"} for key in fields}},
        author=author,author_provider="fixture-author",author_model="complete-v1")
    result = run_checks(database, **options, reverify_all=True)
    assert result["status"] == "complete" and len(calls) == 1, result
    outcome = result["content_enrichment"][0]
    assert outcome["status"] == "pending_review" and outcome["next_owner"] == "G10", outcome
    with sqlite3.connect(database) as db:
        latest, published = db.execute("SELECT latest_candidate_sha256,published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(latest,)).fetchone()[0])
        encoded, frozen_sha = db.execute("SELECT input_json,input_sha256 FROM article_check_runs WHERE run_id=?",(result["run_id"],)).fetchone()
        frozen = json.loads(encoded)
        current = check_targets(database,"articles",{"core:good"})[0]
        partition = partition_items(db,tmp_path/"lifecycle",[outcome["candidate_sha256"]])
        ready = information_ready(db,snapshot)
        print(json.dumps({"initial":initial,"returned":outcome["candidate_sha256"],"latest":latest,"ready":ready,
            "frozen_enrichments":len(frozen["targets"][0]["source_binding"]["enrichments"]),
            "current_enrichments":len(current["source_binding"]["enrichments"]),
            "partition":{key:[{field:item.get(field) for field in ("classification","stage","next_owner","reason")} for item in value] for key,value in partition.items()}}))
        assert frozen["targets"] == [original_target] and frozen_sha == sha(encoded)
        assert current == original_target, "generated output must not change its frozen upstream target"
        assert ready, "the first completed check must be sufficient for the latest candidate"
        assert outcome["candidate_sha256"] == latest, "handoff must identify the terminal candidate"
        assert len(partition["ready"]) == 1 and not partition["held"] and not partition["invalid"], partition
        assert partition["ready"][0]["stage"] == "awaiting_independent_review" and partition["ready"][0]["next_owner"] == "G10"
        assert next(run for run in snapshot["dependencies"]["article_check_runs"] if run["run_id"]==result["run_id"])["status"] == "complete"
        record = snapshot["content_authoring"]
        assert record["request"]["article_body"] == BODY and record["output"]["summary"] == " ".join(BODY.split("\n\n"))
        assert record["request"]["content_version_id"] == ("initial-body" if initial else "body-b")
        validate_content_baselines(db,snapshot,latest)
        if initial:
            rows = [row for row in snapshot["tables"]["article_enrichments"] if row["generator_version"]==VERSION]
            assert len(rows)==1 and rows[0]["summary"]==record["output"]["summary"]
            assert not snapshot.get("derived_display",{}).get("content_update")
        else:
            assert snapshot["derived_display"]["content_update"]["content_version_id"] == "body-b"
            assert snapshot["derived_display"]["content_update"]["change_evidence"]["items"]
            assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(approved,)).fetchone()==old_snapshot
        assert published == approved
        assert set(raw).issubset(db.execute("SELECT * FROM article_content_versions").fetchall())
        assert set(fetches).issubset(db.execute("SELECT * FROM article_fetches").fetchall())
        assert not db.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=?",(latest,)).fetchone()
    repeated = run_checks(database, **options)
    assert repeated["item_count"]==0 and len(calls)==1
    resumed = run_checks(database, **options, resume_run_id=result["run_id"])
    assert resumed["status"]=="complete" and len(calls)==1
    with sqlite3.connect(database) as db:
        final_sha = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0]
        assert resumed["content_enrichment"][0]["candidate_sha256"]==final_sha
        assert partition_items(db,tmp_path/"resumed-lifecycle",[final_sha])["ready"][0]["next_owner"]=="G10"
    if initial:
        with pytest.raises(RegistryNotFoundError):
            _reader(database,tmp_path).article("good")
    else:
        assert _reader(database,tmp_path).article("good")==before_public


def test_g7_output_is_not_upstream_but_real_enrichments_remain_source_bound(tmp_path):
    from test_content_enrichment import BODY
    from test_verified_publication import _database
    from climate_registry.information_checks import check_targets
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        _insert_body(db,"initial-body",BODY,"2026-10-16T00:00:00Z")
        def insert(identity, kind):
            db.execute("""INSERT INTO article_enrichments(enrichment_id,article_id,content_version_id,status,summary,categories_json,
                keywords_json,language,generator_kind,generator_name,generator_version,generated_at)
                VALUES(?,'good','initial-body','complete','Observed source summary','[]','[]','en',?,'existing-source','1','2026-10-16T00:00:00Z')""",(identity,kind))
        insert("source-capture","deterministic")
        insert("source-manual","manual")
    before = check_targets(database,"articles",{"core:good"})[0]
    assert {row["enrichment_id"] for row in before["source_binding"]["enrichments"]}=={"source-capture","source-manual"}
    with sqlite3.connect(database) as db:
        generated = _generated(db,before)
        assert stage(db,before,generated,generated_at="2026-10-17T00:00:00Z")["status"]=="pending_review"
    assert check_targets(database,"articles",{"core:good"})[0]==before
    with sqlite3.connect(database) as db:
        insert("source-later","manual")
    after = check_targets(database,"articles",{"core:good"})[0]
    assert after!=before and {row["enrichment_id"] for row in after["source_binding"]["enrichments"]}=={"source-capture","source-manual","source-later"}



def _r10_linked_core_pdf(tmp_path, monkeypatch, *, ready=True):
    from test_content_enrichment import BODY
    from test_issue112_acquisition import _batch, _item, _database
    from test_pdf_intake import _coverage_layout, _source_decision, _material_bindings
    from climate_registry.acquisition import store_acquisition_batch
    from climate_registry.publication import migrate_publication
    from climate_monitor.pdf_intake import import_pdf_reports, reconcile_source_coverage
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.information_checks import check_targets
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    database = _database(tmp_path)
    migrate_publication(database,tmp_path/"backups",apply=True)
    path = tmp_path/"linked.pdf"; _coverage_layout(path)
    bundle = import_pdf_reports([path]); coverage = bundle["source_coverage"][0]
    reconcile_source_coverage(bundle,[_source_decision(coverage,"r10-linked",_material_bindings(coverage))])
    identity = next(iter(coverage["observation_bindings"]))
    occurrence = next(item for article in bundle["articles"] for item in article["occurrences"] if item["occurrence_id"]==identity)
    url = occurrence["raw_url"]
    persist_pdf_intake(database,tmp_path/"pdf-backups",bundle if ready else {key:value for key,value in bundle.items() if key!="source_coverage"})
    store_acquisition_batch(database,_batch([_item(url=url,body=BODY)]))
    with sqlite3.connect(database) as db:
        core = db.execute("SELECT article_id FROM articles WHERE canonical_url=?",(url,)).fetchone()[0]
        db.execute("UPDATE pdf_intake_articles SET core_article_id=?,confirmation_basis='exact_url_eligible_detail' WHERE canonical_url=?",(core,url))
    targets = [target for target in check_targets(database,"articles") if target["source_url"]==url]
    assert {target["entity_kind"] for target in targets}=={"article","pdf_article"} and len(targets)==2
    return database, core, targets


@pytest.mark.parametrize("mode", ["batch", "limited", "interrupted"])
def test_linked_core_pdf_t1_shares_one_exact_generation_and_final_candidate(tmp_path,monkeypatch,mode):
    from test_content_enrichment import BODY
    from climate_registry.content_enrichment import sha
    from climate_registry.information_checks import run_checks
    from climate_registry.publication import information_ready
    from climate_registry.item_lifecycle import partition_items
    from climate_registry.enrichment_contract import validate_content_baselines
    from test_verified_publication import _reader
    from climate_registry.read_api import RegistryNotFoundError
    database,core,targets = _r10_linked_core_pdf(tmp_path,monkeypatch)
    with sqlite3.connect(database) as db:
        raw = db.execute("SELECT * FROM article_content_versions").fetchall()
        pdf = db.execute("SELECT * FROM pdf_intake_article_occurrences").fetchall()
    calls=[]
    def author(request):
        calls.append(request)
        output=_author(request)
        if len(calls)>1:
            output["summary_items"][0]["statement"]="Current report: "+output["summary_items"][0]["statement"]
            output["summary"]=" ".join(item["statement"] for item in output["summary_items"])
        return output
    options=dict(kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={target["occurrence_id"] for target in targets},
        fetcher=lambda identity,url:{"article_id":identity,"requested_url":url,"final_url":url,"status":"ok","content":BODY,
            "content_ref":None,"content_hash":sha(BODY),"failure_reason":None},
        verifier=lambda kind,fields,body:{"comparisons":{key:{"status":"supported"} for key in fields}},
        author=author,author_provider="fixture-author",author_model="complete-v1")
    try:
        if mode=="interrupted":
            def interrupt(progress):
                if progress.get("occurrence_id")==targets[0]["occurrence_id"]:
                    with sqlite3.connect(database) as check:
                        assert check.execute("SELECT count(*) FROM article_check_attempts").fetchone()[0]==1
                        assert check.execute("SELECT count(*) FROM registry_candidates WHERE json_extract(snapshot_json,'$.content_authoring') IS NOT NULL").fetchone()[0]==0
                    raise InterruptedError("fixture interruption after committed attempt before final staging")
            with pytest.raises(InterruptedError): run_checks(database,**options,reverify_all=True,progress=interrupt)
            with sqlite3.connect(database) as db:
                run_id=db.execute("SELECT run_id FROM article_check_runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            result=run_checks(database,**options,resume_run_id=run_id)
        else:
            result=run_checks(database,**options,reverify_all=True,limit=1 if mode=="limited" else None)
            if mode=="limited":
                assert result["status"]=="pending" and result["completed_count"]==1
                result=run_checks(database,**options,resume_run_id=result["run_id"])
    except ValueError as exc:
        with sqlite3.connect(database) as db:
            print(json.dumps({"error":str(exc),"author_calls":len(calls),"runs":db.execute("SELECT status,item_count,completed_count FROM article_check_runs").fetchall()}))
        raise
    assert result["status"]=="complete" and result["completed_count"]==2 and len(calls)==1,result
    outcomes=result["content_enrichment"]
    assert len(outcomes)==2 and all(item["status"]=="pending_review" for item in outcomes)
    assert len({item["candidate_sha256"] for item in outcomes})==1
    with sqlite3.connect(database) as db:
        latest,published=db.execute("SELECT latest_candidate_sha256,published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id=?",(core,)).fetchone()
        assert {item["candidate_sha256"] for item in outcomes}=={latest} and published is None
        snapshot=json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(latest,)).fetchone()[0])
        assert information_ready(db,snapshot)
        packets=[json.loads(row[0]) for row in db.execute("SELECT packet_json FROM article_check_attempts WHERE run_id=?",(result["run_id"],))]
        assert len(packets)==2 and {packet["entity_kind"] for packet in packets}=={"article","pdf_article"}
        assert {packet["occurrence_id"] for packet in packets}=={target["occurrence_id"] for target in targets}
        assert packets[0]["source_binding"]!=packets[1]["source_binding"]
        assert packets[0]["content_enrichment"]["record"]==packets[1]["content_enrichment"]["record"]==snapshot["content_authoring"]
        assert snapshot["derived_display"]["summary"]==BODY.replace("\n\n"," ")
        validate_content_baselines(db,snapshot,latest)
        partition=partition_items(db,tmp_path/"lifecycle",[latest])
        assert partition["ready"][0]["next_owner"]=="G10" and not partition["held"] and not partition["invalid"]
        assert set(raw).issubset(db.execute("SELECT * FROM article_content_versions").fetchall())
        assert db.execute("SELECT * FROM pdf_intake_article_occurrences").fetchall()==pdf
        assert not db.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=?",(latest,)).fetchone()
    assert run_checks(database,**options)["item_count"]==0 and len(calls)==1
    with pytest.raises(RegistryNotFoundError): _reader(database,tmp_path).article(core)


def test_shared_core_result_never_bypasses_pdf_coverage_gate(tmp_path,monkeypatch):
    from test_content_enrichment import BODY
    from climate_registry.content_enrichment import sha
    from climate_registry.information_checks import run_checks
    database,core,targets=_r10_linked_core_pdf(tmp_path,monkeypatch,ready=False)
    calls=[]
    def author(request): calls.append(request); return _author(request)
    result=run_checks(database,kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={target["occurrence_id"] for target in targets},reverify_all=True,
        fetcher=lambda identity,url:{"article_id":identity,"requested_url":url,"final_url":url,"status":"ok","content":BODY,"content_ref":None,"content_hash":sha(BODY),"failure_reason":None},
        verifier=lambda kind,fields,body:{"comparisons":{key:{"status":"supported"} for key in fields}},author=author,author_provider="fixture-author",author_model="complete-v1")
    assert result["status"]=="partial" and result["completed_count"]==2 and len(calls)==1,result
    pdf=next(item for item in result["content_enrichment"] if item["occurrence_id"]==next(target["occurrence_id"] for target in targets if target["entity_kind"]=="pdf_article"))
    assert pdf["status"]=="held" and "pdf_source_coverage" in pdf["reason"]


def test_taxonomy_change_with_template_noop_stays_editorially_unfinished(tmp_path,monkeypatch):
    from test_content_enrichment import BODY
    from climate_registry.content_enrichment import VERSION,PROMPT,sha,validate_noop
    from climate_registry.acquisition_review import digest
    from climate_registry.information_checks import run_checks
    from climate_registry.item_lifecycle import partition_items
    from climate_registry.publication import prepare_review
    from test_verified_publication import _reader
    import climate_monitor.taxonomy as taxonomy
    database,_=_setup(tmp_path)
    with sqlite3.connect(database) as db:
        assert stage(db,_target(database),_generated(db,_target(database)),generated_at="2026-10-17T00:00:00Z")["status"]=="pending_review"
    _native_contract_review(database,tmp_path,monkeypatch)
    before=_reader(database,tmp_path).article("good")
    original=taxonomy.load_article_taxonomy()
    changed=taxonomy._parse_article_taxonomy_bytes(original.definition_yaml.replace("label: Physical Risk","label: Catastrophe Exposure").encode())
    monkeypatch.setattr(taxonomy,"load_article_taxonomy",lambda *args:changed)
    body=BODY+"\n\nNavigation: subscribe to alerts."
    with sqlite3.connect(database) as db:
        baseline=db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0]
        old=db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(baseline,)).fetchone()[0]
        _insert_body(db,"taxonomy-template-b",body,"2026-10-21T00:00:00Z")
        _,request=prepare(db,_target(database))
    def response(request):
        return {"request_sha256":request["request_sha256"],"entity_type":"Article","substantive_change":False,
            "change_items":[],"no_change_reason":"Only navigation/template wording changed.","no_change_evidence":"Navigation: subscribe to alerts."}
    calls=[]
    def author(request): calls.append(request); return response(request)
    record={"schema_version":VERSION,"request":request,"output":response({**request,"request_sha256":digest(request)}),
        "provider":"fixture-author","model":"complete-v1","prompt_sha256":sha(PROMPT),"runtime_binding":None}
    with sqlite3.connect(database) as db:
        try:
            validate_noop(db,_target(database),{"status":"no-op","record":record,"taxonomy_sha256":changed.sha256})
            rejected=False
        except ValueError as exc:
            rejected="taxonomy" in str(exc)
    options=dict(kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={"core:good"},
        fetcher=lambda identity,url:{"article_id":identity,"requested_url":url,"final_url":url,"status":"ok","content":body,"content_ref":None,"content_hash":sha(body),"failure_reason":None},
        verifier=lambda kind,fields,body:{"comparisons":{key:{"status":"supported"} for key in fields}},author=author,author_provider="fixture-author",author_model="complete-v1")
    result=run_checks(database,**options,reverify_all=True)
    repeated=run_checks(database,**options)
    print(json.dumps({"first":result["status"],"outcome":result["content_enrichment"][0]["status"],"repeat_count":repeated["item_count"],"noop_rejected":rejected}))
    assert rejected and result["status"]=="partial" and result["content_enrichment"][0]["status"]=="held",result
    assert "taxonomy" in result["content_enrichment"][0]["reason"] and "body" in result["content_enrichment"][0]["reason"]
    assert "record" not in result["content_enrichment"][0] and repeated["item_count"]==1 and len(calls)==2
    with sqlite3.connect(database) as db:
        latest,published=db.execute("SELECT latest_candidate_sha256,published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()
        snapshot=json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(latest,)).fetchone()[0])
        assert published==baseline and db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(baseline,)).fetchone()[0]==old
        assert not snapshot.get("derived_display",{}).get("content_update")
        assert "Physical Risk" not in snapshot.get("derived_display",{}).get("categories",[])
        partition=partition_items(db,tmp_path/"taxonomy-lifecycle",[latest])
        assert partition["held"] and not partition["ready"]
        assert partition["held"][0]["stage"]!="no_change"
    assert _reader(database,tmp_path).article("good")==before
    assert not any(item["entity_id"]=="good" for item in prepare_review(database,tmp_path/"taxonomy-review")["candidates"])


@pytest.mark.parametrize("old,new", [
    ("Change is -USD 20.","Change is +USD 20."), ("Change is -USD 20.","Change is USD 20."),
    ("Change is -EUR .5.","Change is +EUR .5."), ("Change is -GBP 20.","Change is GBP 20."),
    ("Change is -USD20.","Change is +USD20."), ("Change is USD -20.","Change is USD +20."),
])
def test_word_scalar_signs_remain_material(old,new):
    assert _body_text(old)!=_body_text(new)


@pytest.mark.parametrize("fact", ["USD 20","20 percent","20 bps","20 USD","20"])
def test_word_scalar_and_numeric_core_share_leading_minus_ambiguity(fact):
    assert _body_text("- "+fact)!=_body_text("-"+fact)
    assert _body_text("- "+fact)!=_body_text(fact)
    assert _body_text("Change is - "+fact)==_body_text("Change is -"+fact)
    assert _body_text("- Loss is "+fact)==_body_text("Loss is "+fact)


@pytest.mark.parametrize("raw", [
    "<p>Loss change is -<span>USD 20</span>.</p>", "<p>Loss change is +<span>USD 20</span>.</p>",
    "<p>- 20 percent</p>", "<ul><li>20 percent</li></ul>",
])
def test_word_scalar_actual_capture_and_acquisition_keep_literal_input(tmp_path,raw):
    from climate_registry.capture import extract_html
    from climate_registry.acquisition import store_acquisition_batch,load_acquisition_batch
    from test_issue112_acquisition import _database,_batch,_item
    body=extract_html(raw.encode(),base_url="https://example.org/article",charset="utf-8")
    database=_database(tmp_path)
    store_acquisition_batch(database,_batch([_item(body=body)]))
    assert load_acquisition_batch(database,"batch-112")["items"][0]["markdown_content"]==body
    assert _body_text(raw)==_body_text(body) if not raw.startswith("<ul>") else _body_text(body)!=_body_text("-20 percent")
    if "USD" in body:
        assert _body_text(body)!=_body_text(body.replace("-USD","USD").replace("+USD","USD"))
    else:
        assert _body_text(body)!=_body_text("-20 percent")


@pytest.mark.parametrize("case", ["update-minimal","update-omitted","update-wrong-time","update-no-date","update-manual","initial-full","initial-minimal","initial-wrong-url","initial-no-date","initial-month","initial-iso","update-invalid"])
def test_article_date_handoff_uses_retained_capture_and_initial_fields(tmp_path,monkeypatch,case):
    from test_content_enrichment import BODY
    from climate_registry.content_enrichment import sha
    from climate_registry.information_checks import run_checks,check_targets
    from climate_registry.enrichment_contract import validate_content_baselines,review_projection
    from climate_registry.article_dates import content_date_projection
    from test_verified_publication import _reader
    initial=case.startswith("initial")
    no_date=case.endswith("no-date")
    month=case=="initial-month"
    field="revision_date" if month else "updated_date"
    text="October 2026" if month else "2026-10-21" if case=="initial-iso" else "2026-02-31" if case=="update-invalid" else "21 October 2026"
    value="2026-10-15" if month else "2026-10-21"
    body=BODY+"\n\nNew coastal adaptation guidance."+("" if no_date else "\n\n"+("Revised: " if month else "Updated: ")+text+("T00:00:00Z" if case=="initial-iso" else ""))
    observed="2026-10-22T04:23:19Z"
    if initial:
        from test_issue112_acquisition import _database
        from test_issue136_meetings import _stored_item,_stored_batch
        from climate_registry.acquisition import store_acquisition_batch
        from climate_registry.publication import migrate_publication
        database=_database(tmp_path);migrate_publication(database,tmp_path/"backups",apply=True)
        url="https://example.org/r11-initial"
        item=_stored_item(url,body,observed)
        item.update(published_date=None,publication_date_evidence=None)
        item["evidence"]["final_url"]=url+"/full"
        store_acquisition_batch(database,_stored_batch("r11-initial","2026-10-22",observed,item))
        target=next(t for t in check_targets(database,"articles") if t["source_url"]==url)
        published=None
    else:
        database,published=_setup(tmp_path)
        if case=="update-manual":
            from test_content_enrichment import _ingest,_manual
            manual=_ingest(database,tmp_path,_manual(database,fields={"updated_date":"2026-10-25"}))
            published=manual["candidate_sha256"]
        with sqlite3.connect(database) as db:
            _insert_body(db,"r11-date-body",body,observed)
        target=_target(database)
    with sqlite3.connect(database) as db:
        raw=db.execute("SELECT * FROM article_content_versions").fetchall()
        fetches=db.execute("SELECT * FROM article_fetches").fetchall()
        old=db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(published,)).fetchone() if published else None
        _,request=prepare(db,target)
        cursor=db.execute("SELECT final_url,fetched_at FROM article_fetches WHERE content_version_id=? AND fetch_status='success' AND http_status BETWEEN 200 AND 299",(request["content_version_id"],))
        final_url,captured=cursor.fetchone()
    calls=[]
    def author(request):
        calls.append(request)
        output=_author({**request,"date_context":{**request["date_context"],"required_dates":[]}}) if case=="update-invalid" else _author(request)
        output["date_evidence"]={}
        if not no_date and case not in {"update-omitted","update-invalid"}:
            proof={"date_kind":"revision" if month else "updated","text":text,"value":value,"source_date_precision":"month" if month else "day"}
            if month:proof["date_normalization"]="month_midpoint"
            if case in {"initial-full","initial-month","update-wrong-time","initial-wrong-url"}:
                proof.update(content_version_id=request["content_version_id"],source_url=request["source_url"],final_url=final_url,captured_at=captured)
            if case=="update-wrong-time":proof["captured_at"]="2026-10-20T00:00:00Z"
            if case=="initial-wrong-url":proof["final_url"]="https://example.org/not-the-capture"
            output["date_evidence"]={field:proof}
        return output
    result=run_checks(database,kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={target["occurrence_id"]},reverify_all=True,
        fetcher=lambda identity,url:{"article_id":identity,"requested_url":url,"final_url":final_url,"status":"ok","content":body,"content_ref":None,"content_hash":sha(body),"failure_reason":None},
        verifier=lambda kind,fields,text:{"comparisons":{key:{"status":"supported"} for key in fields}},author=author,author_provider="fixture-author",author_model="r11-dates")
    outcome=result["content_enrichment"][0]
    print(json.dumps({"case":case,"status":result["status"],"outcome":outcome,"request_keys":sorted(calls[0]),"actual_capture":[final_url,captured]}))
    held=case in {"update-omitted","update-wrong-time","initial-wrong-url","update-invalid"}
    if held:
        assert result["status"]=="partial" and outcome["status"]=="held",result
        assert "date" in outcome["reason"]
    else:
        assert result["status"]=="complete" and outcome["status"]=="pending_review",result
        with sqlite3.connect(database) as db:
            snapshot=json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(outcome["candidate_sha256"],)).fetchone()[0])
            display=snapshot["derived_display"]
            validate_content_baselines(db,snapshot,outcome["candidate_sha256"])
            contract=review_projection(snapshot)
            if no_date:
                projection=contract["date_and_sort_basis"]
                assert projection["content_date_basis"]==("collection_time" if initial else "content_version_discovery")
            else:
                proof=snapshot["content_authoring"]["output"]["date_evidence"][field]
                assert proof["final_url"]==final_url and proof["captured_at"]==observed
                assert proof["content_version_id"]==request["content_version_id"] and proof["source_url"]==request["source_url"]
                if initial:
                    assert display[field]==value and display[field+"_evidence"]==proof
                    assert contract["proposed"]["derived_display"][field]==value
                    assert not display.get("content_update") and snapshot["enrichment_scope"]["scope"]=="initial_article_content"
                    # Existing G1 intentionally does not activate initial dynamic Updated/Revision sorting.
                    assert contract["date_and_sort_basis"]["content_date_basis"]=="collection_time"
                else:
                    assert display["content_update"]["date_evidence"][field]==proof
                    assert display["content_update"]["first_observed_at"]==observed
                    projection=content_date_projection({"article_id":target["entity_id"],**display},snapshot)
                    assert projection["content_date"]==("2026-10-25" if case=="update-manual" else value)
                assert calls[0].get("date_context",{}).get("captures"),"actual retained capture metadata must be exposed"
            assert not db.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=?",(outcome["candidate_sha256"],)).fetchone()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id=?",(target["entity_id"],)).fetchone()[0]==published
        if old:assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(published,)).fetchone()==old
        assert set(raw).issubset(db.execute("SELECT * FROM article_content_versions").fetchall())
        assert set(fetches).issubset(db.execute("SELECT * FROM article_fetches").fetchall())
    if initial and not held and not no_date:
        from climate_registry.publication import export_public_snapshot
        from climate_registry.read_api import RegistryReader
        _native_contract_review(database,tmp_path,monkeypatch,identity=target["entity_id"])
        reader=_reader(database,tmp_path)
        public=reader.article(target["entity_id"])
        assert public[field]==value and public[field+"_evidence"]["captured_at"]==observed
        exported=tmp_path/"approved-export";(exported/"wiki").mkdir(parents=True)
        export_public_snapshot(database,exported/"wiki/public-registry.json",reader=reader)
        static=RegistryReader.from_public_export(exported)
        assert static.article(target["entity_id"])[field]==value
        assert not static.article(target["entity_id"]).get("content_update")


@pytest.mark.parametrize("label", ["Updated", "Revised", "Published", "Posted"])
@pytest.mark.parametrize("material", [False, True])
def test_iso_timestamp_only_change_is_metadata_in_real_t1(tmp_path,monkeypatch,label,material):
    from test_content_enrichment import BODY
    from climate_registry.content_enrichment import sha
    from climate_registry.information_checks import run_checks
    from climate_registry.publication import prepare_review
    from test_verified_publication import _reader
    database,_=_setup(tmp_path)
    original=BODY+"\n\n"+label+": 2026-10-21T00:00:00Z"
    with sqlite3.connect(database) as db:
        _insert_body(db,"iso-source-a",original,"2026-10-22T00:00:00Z")
        accepted=stage(db,_target(database),_generated(db,_target(database)),generated_at="2026-10-22T00:00:01Z")
        assert accepted["status"]=="pending_review",accepted
    _native_contract_review(database,tmp_path,monkeypatch)
    public=_reader(database,tmp_path).article("good")
    current=original.replace("T00:00:00Z","T01:00:00Z")
    if material:current=current.replace("20 billion","40 billion")
    with sqlite3.connect(database) as db:
        published=db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0]
        previous=db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(published,)).fetchone()[0]
        raw=db.execute("SELECT * FROM article_content_versions").fetchall()
        fetches=db.execute("SELECT * FROM article_fetches").fetchall()
        enrichments=db.execute("SELECT * FROM article_enrichments").fetchall()
        reviews=db.execute("SELECT * FROM registry_reviews").fetchall()
        _insert_body(db,"iso-source-b",current,"2026-10-23T00:00:00Z")
    calls=[]
    def author(request):
        calls.append(request);output=_author(request)
        output["change_items"]=[{"note":"The source now reports the current loss amount.","basis":"body_comparison","previous_excerpt":original,"current_excerpt":current}]
        return output
    options=dict(kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={"core:good"},
        fetcher=lambda identity,url:{"article_id":identity,"requested_url":url,"final_url":url,"status":"ok","content":current,"content_ref":None,"content_hash":sha(current),"failure_reason":None},
        verifier=lambda kind,fields,text:{"comparisons":{key:{"status":"supported"} for key in fields}},author=author,author_provider="fixture-author",author_model="complete-v1")
    result=run_checks(database,**options,reverify_all=True)
    outcome=result["content_enrichment"][0]
    print(json.dumps({"label":label,"material":material,"status":result["status"],"outcome":outcome,"author_calls":len(calls)}))
    assert result["status"]=="complete",result
    assert outcome["status"]==("pending_review" if material else "no-op"),outcome
    assert len(calls)==int(material)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good'").fetchone()[0]==published
        assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(published,)).fetchone()[0]==previous
        assert set(raw).issubset(db.execute("SELECT * FROM article_content_versions").fetchall())
        assert set(fetches).issubset(db.execute("SELECT * FROM article_fetches").fetchall())
        assert db.execute("SELECT * FROM registry_reviews").fetchall()==reviews
        if not material:assert db.execute("SELECT * FROM article_enrichments").fetchall()==enrichments
        else:
            snapshot=json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(outcome["candidate_sha256"],)).fetchone()[0])
            bundle=snapshot["derived_display"]["content_update"]
            if label in {"Updated", "Revised"}:
                field="updated_date" if label=="Updated" else "revision_date"
                assert bundle["date_evidence"][field]["text"]=="2026-10-21"
                assert bundle["date_evidence"][field]["captured_at"]=="2026-10-23T00:00:00Z"
            else:
                assert bundle["date_evidence"]=={}  # Published/Posted handoff remains independent.
            assert "40 billion" in bundle["summary"] and bundle["change_evidence"]["items"]
    packet=prepare_review(database,tmp_path/"iso-review")
    if not material:assert not any(candidate["entity_id"]=="good" for candidate in packet["candidates"])
    assert _reader(database,tmp_path).article("good")==public
    repeated=run_checks(database,**options)
    assert repeated["item_count"]==0 and len(calls)==int(material)


@pytest.mark.parametrize("label", ["Updated", "Last Updated", "Revised", "Revision Date", "Published", "Posted"])
def test_labelled_iso_clock_metadata_preserves_day_and_same_line_facts(label):
    from climate_registry.article_dates import labelled_source_dates
    old=label+": 2026-10-21T00:00:00Z - Flood losses reached 20 billion."
    new=old.replace("T00:","T01:")
    assert _body_text(old)==_body_text(new)
    assert _body_text(old)!=_body_text(new.replace("20 billion","40 billion"))
    selected=labelled_source_dates(new)
    assert len(selected)==1 and selected[0]["text"]=="2026-10-21"
    assert selected[0]["valid"] and selected[0]["source_date_precision"]=="day"
