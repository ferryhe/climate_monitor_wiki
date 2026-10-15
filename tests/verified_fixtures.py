"""Source-bound T1 evidence for fixtures whose intended state is publicly verified."""
import hashlib
import json
import uuid

from climate_registry.information_checks import CHECK_VERSION, _json, _sha, _now, evaluate, information_targets


def _needs_check(connection, prefix, target):
    column = "occurrence_id" if target["entity_kind"].startswith("pdf_") else "core_article_id" if prefix == "article" else "event_source_id"
    identity = target["entity_id"] if column == "core_article_id" else target["occurrence_id"]
    row = connection.execute(f"SELECT packet_json,packet_sha256 FROM {prefix}_check_attempts WHERE {column}=? AND source_url=? ORDER BY checked_at DESC,rowid DESC LIMIT 1", (identity,target["source_url"])).fetchone()
    if not row:
        return True
    packet = json.loads(row[0])
    return not (packet.get("schema_version") == CHECK_VERSION and packet.get("verification_status") == "verified"
        and _sha(packet) == row[1] and all(packet.get(key) == target[key] for key in ("source_revision_sha256", "source_binding", "fields")))


def seed_verified_articles(connection, *, occurrence_ids=None):
    """Use the real evaluator and immutable run/attempt contract on isolated data."""
    if connection.execute('PRAGMA user_version').fetchone()[0] < 22:
        return
    targets = information_targets(connection, 'articles')
    if occurrence_ids is not None:
        targets = [target for target in targets if target['occurrence_id'] in occurrence_ids]
    targets = [target for target in targets if _needs_check(connection, "article", target)]
    if not targets:
        return
    stamp = _now()
    run = 'fixture-check-' + uuid.uuid4().hex
    frozen = {'schema_version': CHECK_VERSION, 'kind': 'articles', 'targets': targets, 'retry_of': None,
        'registry_database': connection.execute('PRAGMA database_list').fetchone()[2]}
    encoded = _json(frozen)
    connection.execute("INSERT INTO article_check_runs(run_id,input_json,input_sha256,created_at,status,item_count,completed_count,completed_at) VALUES(?,?,?,?,'complete',?,?,?)",
        (run, encoded, hashlib.sha256(encoded.encode()).hexdigest(), stamp, len(targets), len(targets), stamp))
    for target in targets:
        body = '\n'.join(str(value) for value in target['fields'].values() if value is not None) or 'Fixture complete source article.'
        if target['entity_kind']=='article':
            acquisition=target['source_binding'].get('acquisition') or []
            selected=next((row['content_version_id'] for row in reversed(acquisition) if row.get('content_version_id') and row.get('processing_status')=='complete'),None)
            content=connection.execute('SELECT markdown_content FROM article_content_versions WHERE content_version_id=COALESCE(?,(SELECT current_content_version_id FROM articles WHERE article_id=?))',(selected,target['entity_id'])).fetchone()
            if content and content[0]:body=content[0]
        record = {'article_id': target['occurrence_id'], 'requested_url': target['source_url'], 'final_url': target['source_url'],
            'status': 'ok', 'content': body, 'content_ref': None, 'content_hash': hashlib.sha256(body.encode()).hexdigest()}
        packet = evaluate(target, 'articles', record,
            lambda kind, fields, content: {'comparisons': {key: {'status': 'supported'} for key in fields}})
        assert packet['verification_status'] == 'verified', packet
        native = target['entity_kind'] == 'article'
        connection.execute("INSERT INTO article_check_attempts(attempt_id,run_id,occurrence_id,source_url,source_revision_sha256,checked_at,access_status,verification_status,packet_json,packet_sha256,core_article_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ('fixture-attempt-' + uuid.uuid4().hex, run, None if native else target['occurrence_id'], target['source_url'],
             target['source_revision_sha256'], packet['checked_at'], packet['access_status'], packet['verification_status'],
             _json(packet), _sha(packet), target['entity_id'] if native else None))


def seed_verified_native_meetings(connection, *, occurrence_ids=None):
    """Check each native meeting against its retained, extracted source body."""
    if connection.execute('PRAGMA user_version').fetchone()[0] < 22:
        return
    targets = [target for target in information_targets(connection, 'meetings') if target['entity_kind'] == 'meeting']
    if occurrence_ids is not None:
        targets = [target for target in targets if target['occurrence_id'] in occurrence_ids]
    for target in targets:
        if not _needs_check(connection, "meeting", target):
            continue
        source = connection.execute('SELECT candidate_json,content_version_id FROM climate_event_sources WHERE event_source_id=?', (target['occurrence_id'],)).fetchone()
        body = connection.execute('SELECT markdown_content FROM article_content_versions WHERE content_version_id=?', (source[1],)).fetchone()[0]
        from climate_monitor.meetings import EXTRACTION_FIELDS
        extracted = json.loads(source[0])
        candidate = {key: extracted.get(key) for key in EXTRACTION_FIELDS}
        packet = evaluate(target, 'meetings', {'article_id': target['occurrence_id'], 'requested_url': target['source_url'],
            'final_url': target['source_url'], 'status': 'ok', 'content': body, 'content_ref': None,
            'content_hash': hashlib.sha256(body.encode()).hexdigest()},
            lambda kind, fields, content: {'comparisons': {key: {'status': 'supported'} for key in fields}, 'website_candidate': candidate})
        assert packet['verification_status'] == 'verified', packet
        run = 'fixture-meeting-' + uuid.uuid4().hex
        frozen = _json({'schema_version': CHECK_VERSION, 'kind': 'meetings', 'targets': [target], 'retry_of': None,
            'registry_database': connection.execute('PRAGMA database_list').fetchone()[2]})
        connection.execute("INSERT INTO meeting_check_runs VALUES(?,?,?,?,?,'complete',1,1,NULL)",
            (run, frozen, hashlib.sha256(frozen.encode()).hexdigest(), packet['checked_at'], packet['checked_at']))
        connection.execute("INSERT INTO meeting_check_attempts(attempt_id,run_id,occurrence_id,source_url,source_revision_sha256,checked_at,access_status,verification_status,packet_json,packet_sha256,event_source_id) VALUES(?,?,NULL,?,?,?,?,?,?,?,?)",
            ('fixture-meeting-attempt-' + uuid.uuid4().hex,run,target['source_url'],target['source_revision_sha256'],
             packet['checked_at'],packet['access_status'],packet['verification_status'],_json(packet),_sha(packet),target['occurrence_id']))


def seed_verified_pdf_meetings(connection):
    """Real evaluator proof for PDF rows intended as visible fixture facts."""
    if connection.execute('PRAGMA user_version').fetchone()[0] < 22:
        return
    from climate_monitor.meetings import EXTRACTION_FIELDS, EVENT_TYPES
    for target in information_targets(connection, 'meetings'):
        if target['entity_kind'] != 'pdf_meeting' or not _needs_check(connection,'meeting',target):
            continue
        fields=target['fields']
        candidate={key:fields.get(key) for key in EXTRACTION_FIELDS}
        candidate.update(event_type=fields.get('event_type') if fields.get('event_type') in EVENT_TYPES else 'meeting',
            status=fields.get('status') or 'scheduled',timezone=fields.get('event_timezone'),
            date_evidence=fields.get('raw_date') or fields.get('start_date'),status_evidence=None)
        if candidate.get('deadline_date'):
            candidate['deadline_evidence']=candidate['deadline_date']
        evidence=candidate.get('date_evidence') or ''
        body=f"{candidate['name']} is held {evidence}."
        if candidate.get('deadline_date'):
            body+=f" {candidate['name']} {candidate['deadline_type']} deadline {candidate['deadline_date']}."
        body+='\n'+'\n'.join(str(value) for value in candidate.values() if value is not None)
        packet=evaluate(target,'meetings',{'article_id':target['occurrence_id'],'requested_url':target['source_url'],
            'final_url':target['source_url'],'status':'ok','content':body,'content_ref':None,
            'content_hash':hashlib.sha256(body.encode()).hexdigest()},
            lambda kind,fields,content:{'comparisons':{key:{'status':'supported'} for key in fields},'website_candidate':candidate})
        assert packet['verification_status']=='verified',packet
        run='fixture-pdf-meeting-'+uuid.uuid4().hex
        frozen=_json({'schema_version':CHECK_VERSION,'kind':'meetings','targets':[target],'retry_of':None,
            'registry_database':connection.execute('PRAGMA database_list').fetchone()[2]})
        connection.execute("INSERT INTO meeting_check_runs VALUES(?,?,?,?,?,'complete',1,1,NULL)",
            (run,frozen,hashlib.sha256(frozen.encode()).hexdigest(),packet['checked_at'],packet['checked_at']))
        connection.execute("INSERT INTO meeting_check_attempts(attempt_id,run_id,occurrence_id,source_url,source_revision_sha256,checked_at,access_status,verification_status,packet_json,packet_sha256) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ('fixture-pdf-attempt-'+uuid.uuid4().hex,run,target['occurrence_id'],target['source_url'],target['source_revision_sha256'],
             packet['checked_at'],packet['access_status'],packet['verification_status'],_json(packet),_sha(packet)))
