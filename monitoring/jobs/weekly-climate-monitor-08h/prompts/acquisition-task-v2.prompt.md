<!-- acquisition-protocol:trusted-search-ledger.v2 -->
Guide one frozen native-search weekly climate acquisition before report preparation.

Use the immutable run binding for source scope, date policy, and controlled fetch/runtime limits. Reference the configured `search_guidance`, `relevance`, `article_summary`, and `executive_summary` components by their bound IDs and hashes; do not copy their text into this task.

This task is using `trusted-search-ledger.v2`. Use Hermes native `web_search` for justified discovery within the frozen source scope, and use the available public web/browser tools to inspect relevant results. Do not call `climate_stage_candidate` or `climate_finalize_candidate`: those are V3 candidate-handle operations. The trusted V2 runner records every actual search call and its exact result references in the search ledger; never invent a search, query, result, or ledger record. A failed search is not zero results, and a defensible no-search decision needs a concrete coverage reason.

For each relevant search result in the exact frozen source scope, provide the URL and evidence in the bound V2 response contract. Preserve exact source identity, query/result provenance, publication-date evidence, actual fetch attempts, failures, missing coverage, and next steps. Never use event, discovery, issue, or fetch dates as publication dates. Unknown publication dates remain pending review when a date window is enabled. The trusted runner performs controlled reads, validates and stores the batch, reads it back, and freezes compatible report input; do not claim those steps from prose.

Do not expand the frozen source scope or budgets, crawl rotation sites, publish, deliver, bypass report gates, create a queue, or introduce another search/fetch implementation.
<!-- /acquisition-protocol:trusted-search-ledger.v2 -->
<!-- acquisition-protocol:trusted-candidate-handles.v3 -->
Guide one trusted-candidate-handles.v3 climate-monitor acquisition run before report preparation.

Follow the immutable run binding's source scope, date policy, and controlled fetch/runtime limits. Reference the configured `search_guidance`, `relevance`, `article_summary`, and `executive_summary` components by their bound IDs and hashes; do not copy their text into this task.

Use the supplemental per-source skill hints only to interpret the exact governed candidates supplied by web_listening. They are not evidence and cannot expand the frozen source scope, change reader policy, or override actual governed-reader outcomes.

Use only the exact web_listening candidate handles supplied in the trusted run context. Do not run Hermes native search or browser reads. For each relevant candidate within its exact reviewed source scope, call climate_stage_candidate with that result_handle and exact source_key. The tool owns URL binding, publication-date policy, governed article reading, and its durable receipt. Then call climate_finalize_candidate with the returned candidate_handle and only the bounded relevance annotations it requests. If a source has no governed candidate, preserve that coverage gap.

Do not copy, infer, or submit raw tool-call identities, search records, publication dates, article bodies, hashes, content references, snapshots, or Registry fields. Do not stage an unreviewed host or rewrite a result URL.

The trusted runner validates the durable search and candidate receipts, stores and reads back the existing Registry batch, and freezes the report input. Do not perform those operations or claim their completion. Do not publish, deliver, bypass report gates, create a queue, or introduce another search/fetch implementation.
<!-- /acquisition-protocol:trusted-candidate-handles.v3 -->
