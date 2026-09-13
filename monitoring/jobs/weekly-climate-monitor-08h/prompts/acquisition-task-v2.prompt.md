Guide one trusted-candidate-handles.v3 climate-monitor acquisition run before report preparation.

Follow the immutable run binding's source scope, date policy, and controlled fetch/runtime limits. Reference the configured `search_guidance`, `relevance`, `article_summary`, and `executive_summary` components by their bound IDs and hashes; do not copy their text into this task.

Use Hermes native search when it can resolve a coverage gap. Search results retain their public query, URL, title, and snippet and expose ordered result handles. For a relevant result within its exact reviewed source scope, call climate_stage_candidate with that result_handle and exact source_key. The tool owns search-result binding, publication-date policy, governed article reading, and its durable receipt. Then call climate_finalize_candidate with the returned candidate_handle and only the bounded relevance annotations it requests.

Do not copy, infer, or submit raw tool-call identities, a search ledger, publication dates, article bodies, hashes, content references, snapshots, or Registry fields. Do not stage an unreviewed host or rewrite a result URL. A failed search is not zero results; a completed search with no relevant candidate remains truthful search activity.

The trusted runner validates the durable search and candidate receipts, stores and reads back the existing Registry batch, and freezes the report input. Do not perform those operations or claim their completion. Do not publish, deliver, bypass report gates, create a queue, or introduce another search/fetch implementation.
