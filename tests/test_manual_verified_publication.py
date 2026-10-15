"""Historical manual website evidence stays scoped to its exact approved candidate."""
import hashlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from test_verified_publication import _database, _reader, DOC
from climate_registry.acquisition_review import digest
from climate_registry.information_checks import source_revision, _json
from climate_registry.publication import stage_snapshot, snapshot_entity, _approve, export_public_snapshot
from climate_registry.read_api import RegistryReader
from climate_registry.verification import evaluate_snapshot


def _manual(database, *, access="accessible", title="Reviewed launch", date_excerpt="13 November 2026"):
    url = "https://example.org/manual"
    identity = {"title_excerpt": title, "date_excerpt": date_excerpt, "source_url": url}
    fields = {"location": {"value": "Reviewed venue", "excerpt": "Reviewed venue"}}
    scope = ["identity:title_date", "location"]
    raw = {"event_id": "manual", "occurrence_id": "manual-occ", "source_document_sha256": DOC, "page": 3,
        "name": "Reviewed launch PRIVATE title addition", "raw_date": "13-14 November 2026", "date_precision": "day",
        "start_date": "2026-11-13", "end_date": "2026-11-14", "kind": "conference", "summary": "PRIVATE description",
        "organizer": "PRIVATE organizer", "relevance_reason": "PRIVATE relevance", "online_url": "https://private.test/",
        "source_urls": [url], "location": "Reviewed venue", "raw_text": "PRIVATE PDF passage",
        "website_source_evidence": {url: {"identity_evidence": identity, "source_url": url,
            "evidence_access": "webpage_text" if access == "accessible" else "primary_search_index; direct_webpage_fetch_returned_403"}},
        "website_enrichment_evidence": {"location": {"confirmed_value": "Reviewed venue", "quoted_text": "Reviewed venue", "source_url": url}}}
    revision = source_revision("meetings", raw)
    packet = {"schema_version": "manual-website-verification.v1", "method": "manual_official_website_review",
        "checked_at": "2026-10-12T10:00:00Z", "recorded_at": "2026-10-12T10:01:00Z", "occurrence_id": "manual-occ",
        "source_revision_sha256": revision, "access_status": access, "verification_status": "verified",
        "website_candidate": None, "canonical_event_id": None, "reviewed_scope": scope, "confirmed_field_evidence": fields,
        "date_update_evidence": None, "evidence_reference": {"source_url": url, "facts_sha256": "a"*64,
            "prior_independent_review_sha256": "b"*64, "reviewer_id": "independent-prior-reviewer"},
        "reader": {"method": "manual_official_website_review", "evidence_access": "webpage_text" if access == "accessible" else "primary_search_index; direct_webpage_fetch_returned_403", "identity_evidence": identity,
            "evidence_reference": url, "date_update_evidence": None}}
    frozen = {"schema_version": packet["schema_version"], "kind": "meetings", "run_id": "manual-run",
        "checked_at": packet["checked_at"], "created_at": packet["recorded_at"], "facts_sha256": "a"*64,
        "prior_independent_review_sha256": "b"*64, "prior_reviewer_id": "independent-prior-reviewer",
        "manual_targets": [{"occurrence_id": "manual-occ", "event_id": "manual", "source_url": url,
            **{key: packet[key] for key in ("source_revision_sha256", "access_status", "reviewed_scope", "confirmed_field_evidence", "date_update_evidence")}}]}
    encoded = _json(frozen)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO pdf_intake_calendar_items VALUES('manual-occ','manual',?,3,?,'event',?,'day',?,?,?, ?,NULL,?)",
            (DOC,raw['name'],raw['raw_date'],raw['start_date'],raw['end_date'],raw['summary'],'c'*64,json.dumps(raw)))
        db.execute("INSERT INTO meeting_check_runs VALUES('manual-run',?,?,?,?, 'complete',1,1,NULL)",
            (encoded,hashlib.sha256(encoded.encode()).hexdigest(),packet['recorded_at'],packet['recorded_at']))
        db.execute("INSERT INTO meeting_check_attempts(attempt_id,run_id,occurrence_id,source_url,source_revision_sha256,checked_at,access_status,verification_status,packet_json,packet_sha256) VALUES('manual-check','manual-run','manual-occ',?,?,?,?,?,?,?)",
            (url,revision,packet['checked_at'],access,'verified',_json(packet),digest(packet)))
        snapshot = snapshot_entity(db,'pdf_meeting','manual')
        candidate = stage_snapshot(db,snapshot)
        receipt = {"basis": "Explicit isolated fixture of the retained independent manual review contract; no native T1 claimed",
            "candidate_sha256":candidate,"decision":"pass","entity_kind":"pdf_meeting","event_id":"manual","occurrence_id":"manual-occ",
            "facts_sha256":"a"*64,"reviewer_id":"independent-final-reviewer","reviewer_independent":True,
            "report_sha256":"d"*64,"plan_sha256":"e"*64,"stage_manifest_sha256":"f"*64}
        _approve(db,candidate,receipt,status='accepted_legacy')
        # The real manual contract uses a pass receipt, not accepted_legacy approval.
        db.execute("INSERT INTO registry_reviews VALUES(?,?, 'pass',?,?)", (digest([candidate,'pass',receipt]),candidate,packet['recorded_at'],json.dumps(receipt)))
        reviews = [dict(zip(('review_id','candidate_sha256','status','receipt_json'),row)) for row in db.execute('SELECT review_id,candidate_sha256,status,receipt_json FROM registry_reviews')]
    return snapshot,reviews


def test_manual_scoped_facts_direct_projection_export_range_wiki(tmp_path):
    database=_database(tmp_path);snapshot,reviews=_manual(database)
    original=digest(snapshot)
    verdict=evaluate_snapshot(snapshot,reviews=reviews)
    assert verdict['eligible']
    manual=verdict['primary']['manual_verification']
    assert manual['public_facts']['end_date'] is None
    assert 'manual_date_outside_evidence:end_date' in manual['excluded_fields']
    assert not evaluate_snapshot(snapshot)['eligible']
    reader=_reader(database,tmp_path)
    def verify(current):
        item=current.meetings_all(base_date='2026-10-15')[0]
        assert item['name']=='Reviewed launch' and item['location']=='Reviewed venue'
        assert item['start_date']=='2026-11-13' and item['end_date'] is None
        assert item['verification_status']=='verified' and item['collected_candidate'] is None
        assert not item['organizer'] and not item['summary'] and not item['relevance_reason']
        assert 'PRIVATE' not in json.dumps(item)
        assert item['manual_verification']['verification_basis']=='manual_official_website_review'
        return item
    direct=verify(reader)
    from climate_registry.information_checks import merge_checked_observation
    from climate_monitor.meeting_fields import merge_meeting_observations
    unchecked={**direct,"manual_verification":None,"verification_status":"unchecked","location":"PRIVATE override",
        "summary":"PRIVATE merge text","organizer":"PRIVATE merge organizer","checks":[]}
    unchecked["pdf_observations"]=[{key:value for key,value in unchecked.items() if key!="pdf_observations"}]
    for first,second in ((direct,unchecked),(unchecked,direct)):
        duplicate=merge_checked_observation(first,second)
        assert duplicate["location"]=="Reviewed venue" and not duplicate["summary"]
        assert "PRIVATE" not in json.dumps(duplicate)
        merged=merge_meeting_observations([first,second])[0]
        assert merged["location"]=="Reviewed venue" and not merged["organizer"] and not merged["summary"]
        assert "PRIVATE" not in json.dumps(merged)
    with reader.public_snapshot(): verify(reader)
    root=tmp_path/'static';(root/'wiki').mkdir(parents=True)
    export_public_snapshot(database,root/'wiki/public-registry.json')
    verify(RegistryReader.from_public_export(root))
    from climate_registry.publication import public_wiki_pages
    wiki=public_wiki_pages(database)
    assert 'Reviewed launch' in wiki['registry-meetings.md']
    assert 'PRIVATE' not in '\n'.join(wiki.values())
    from climate_registry.range_reports import freeze_range_report
    report=freeze_range_report(reader,tmp_path/'range',start_date='2026-09-01',end_date='2026-11-30')
    assert report['pdf_calendar']['records'][0]['end_date'] is None
    assert 'PRIVATE' not in json.dumps(report)
    assert digest(snapshot)==original
    with sqlite3.connect(database) as db:
        assert 'PRIVATE' in db.execute("SELECT item_json FROM pdf_intake_calendar_items WHERE occurrence_id='manual-occ'").fetchone()[0]
    from climate_registry.verification_report import write_verification_report
    inventory=json.loads(Path(write_verification_report(database,tmp_path/'runs')['json_path']).read_text())
    row=next(row for row in inventory['rows'] if row['entity_id']=='manual' and row['version']=='published')
    assert row['factual_public'] and row['manual_verification']['public_facts']['end_date'] is None
    assert 'manual_field_outside_reviewed_scope:summary' in row['reason_codes']


@pytest.mark.parametrize('damage',['packet_hash','revision','url','run_target','facts','candidate','occurrence','independence','report_sha'])
def test_manual_evidence_requires_exact_proof_chain(tmp_path,damage):
    snapshot,reviews=_manual(_database(tmp_path));snapshot=deepcopy(snapshot);reviews=deepcopy(reviews)
    attempt=snapshot['tables']['meeting_check_attempts'][0]
    if damage=='packet_hash': attempt['packet_sha256']='0'*64
    elif damage=='revision': attempt['source_revision_sha256']='0'*64
    elif damage=='url': attempt['source_url']='https://other.test/'
    elif damage=='run_target':
        run=snapshot['dependencies']['meeting_check_runs'][0];frozen=json.loads(run['input_json']);frozen['manual_targets'][0]['event_id']='other'
        run['input_json']=_json(frozen);run['input_sha256']=hashlib.sha256(run['input_json'].encode()).hexdigest()
    elif damage=='facts':
        packet=json.loads(attempt['packet_json']);packet['evidence_reference']['facts_sha256']='0'*64
        attempt['packet_json']=_json(packet);attempt['packet_sha256']=digest(packet)
    else:
        row=next(row for row in reviews if row['status']=='pass');receipt=json.loads(row['receipt_json'])
        key={'candidate':'candidate_sha256','occurrence':'occurrence_id','independence':'reviewer_independent','report_sha':'report_sha256'}[damage]
        receipt[key]=False if damage=='independence' else 'wrong'
        row['receipt_json']=json.dumps(receipt);row['review_id']=digest([row['candidate_sha256'],'pass',receipt])
    if damage in {'packet_hash','revision','url','run_target','facts'}:
        for row in reviews:
            receipt=json.loads(row['receipt_json']);row['candidate_sha256']=digest(snapshot)
            receipt['candidate_sha256']=row['candidate_sha256'];row['receipt_json']=json.dumps(receipt)
            row['review_id']=digest([row['candidate_sha256'],row['status'],receipt])
    assert not evaluate_snapshot(snapshot,reviews=reviews)['eligible']


def test_manual_search_index_does_not_claim_successful_webpage_read(tmp_path):
    database=_database(tmp_path);_manual(database,access="unavailable")
    item=_reader(database,tmp_path).meetings_all(base_date="2026-10-15")[0]
    assert item["verification_status"]=="verified" and item["access_status"]=="unavailable"
    assert "direct_webpage_fetch_returned_403" in item["manual_verification"]["evidence_access"]
    assert item["collected_candidate"] is None and not item["summary"]


@pytest.mark.parametrize("title,text,start,end", [
    ("Understanding Risk", "May 10-14, 2027", "2027-05-10", "2027-05-14"),
    ("G20 Leaders", "December 14–15, 2026", "2026-12-14", "2026-12-15"),
    ("IAIS Annual Conference 2026", "12-13 November", "2026-11-12", "2026-11-13"),
    ("Pre-COP ministerial meeting", "5-8 October", None, None),
    ("Reviewed launch", "Dates to be announced", None, None),
])
def test_manual_dates_only_use_explicit_identity_evidence(tmp_path,title,text,start,end):
    snapshot,reviews=_manual(_database(tmp_path),title=title,date_excerpt=text)
    before=digest(snapshot)
    verdict=evaluate_snapshot(snapshot,reviews=reviews)
    assert verdict['eligible']
    facts=verdict['primary']['manual_verification']['public_facts']
    assert (facts['start_date'],facts['end_date'])==(start,end)
    assert facts['raw_date']==text
    assert facts['date_precision']==('day' if start else 'unknown')
    assert digest(snapshot)==before
