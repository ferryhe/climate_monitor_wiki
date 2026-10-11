# Weekly climate monitor contract v2

Use the repository's existing prepare, serial authoring and finalize workflow.
Hermes supplies native model execution; web_listening supplies governed evidence.
Deployment paths, credentials, scheduler identity and runtime limits are local configuration.

Each URL receives an independent context. Use only its frozen evidence and bound
relevance/article_summary components to decide relevance, summarize, classify and
assign keywords. Cite evidence accurately; never invent content or repair missing
evidence with another crawl. Preserve the request's article identities and hashes.

The separate executive invocation sees qualified summaries only and uses the
bound executive_summary component. Search guidance and relevance remain separate
versioned components, frozen with the task and checkpoints for that run.

Return the exact structured response required by the supplied authoring contract
and taxonomy. The existing validator must accept every required article before
finalization writes the report, semantic sidecar, Wiki or dedupe state. Failed or
unfinished URL work blocks finalization and remains resumable under its original
binding. This artifact identifies the weekly contract; it does not replace the
native per-URL or executive prompts or define a live schedule.
