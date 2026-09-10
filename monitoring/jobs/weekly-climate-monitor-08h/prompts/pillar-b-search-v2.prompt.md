# Pillar B v2: native climate and actuarial search handoff

Report date: ${report_date}
Frozen publication-date policy: `${date_policy_json}`

Use any currently installed Hermes Agent search capability when discovery gaps
justify a search. Choose queries from the observed institution/content gaps;
there is no fixed query list and no required query count. Do not build or wrap a
new search tool. ${search_time_guidance}

Search may cover articles, papers, meeting minutes, webinars, and events, but an
event date, issue date, search date, or fetch date is never an article's
publication date. Keep an unknown publication date as `null`; never invent one.

Assign each real search attempt a batch-unique `search_ref`. Preserve every real
query, engine, result reference, budget, and success/failure. Each article must
repeat the `search_ref` of the successful attempt that returned its `result_ref`.
A failure is not a successful zero-result search. If coverage makes search
unnecessary, use `no_search` with a concrete coverage reason and no attempts.

Write UTF-8 JSON to exactly ${output_path_json} using this envelope:

```json
{"schema_version":"pillar-b-discovery.v2","report_date":"${report_date}","date_policy":${date_policy_json},"search_decision":{"status":"attempted","reason":null},"searches":[{"search_ref":"search-1","query":"agent-chosen executed query","engine":"installed tool name","status":"success","attempted_at":"RFC3339","result_refs":["provider result URL/id"],"budget":{"max_results":5,"used_results":1},"error":null}],"articles":[{"title":"source title","url":"https://publisher/article","source":"publisher","summary":"source-backed excerpt","published_date":null,"date_evidence":null,"search_ref":"search-1","result_ref":"provider result URL/id"}]}
```

For a known date, `date_evidence` is
`{"kind":"publisher|search_result","url":"the same article URL","text":"verbatim evidence"}`.
Store all relevant candidates in the envelope, including unknown/out-of-window
rows; the frozen policy controls report selection after database persistence.
Do not author the report, publish, send messages, or mutate seen state here.
