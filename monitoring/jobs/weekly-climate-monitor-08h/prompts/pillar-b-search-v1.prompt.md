# Pillar B: climate and actuarial intelligence search

Report date: ${report_date}
Article date window: ${window_start} through ${report_date}, inclusive

Use web_search to discover articles, research and institutional reports relevant
to climate change AND actuarial or insurance risk. Execute every query below.
Apply the tool's publication-date filter when supported. In all cases, verify
the article date from the publisher page or a dated source-backed search result.

## Search queries

- climate change actuarial risk insurance disclosure after:${search_start} before:${search_end}
- IFRS S2 ISSB climate disclosure actuary after:${search_start} before:${search_end}
- parametric insurance climate adaptation after:${search_start} before:${search_end}
- climate risk scenario actuarial after:${search_start} before:${search_end}

## Selection rules

- Keep only articles published within the article date window above. Search
  filters alone are not proof of an article's date. Never use discovery time,
  copyright year, an upcoming event date, or a generic page refresh as publication.
- Exclude older articles, future-dated articles and articles whose publication
  date cannot be established. Do not infer an exact date from the URL alone.
- Prefer the original publisher, actuarial bodies, insurance supervisors and
  research institutions. Keep article/report pages, not homepages or topic indexes.
- Deduplicate by canonical article URL. There is no target item quota or fixed
  article-count cap; execute the full query set and retain every qualifying result.
- Preserve the source title and institution/website name. Do not invent dates,
  titles, URLs or facts, and do not pad a sparse result set with older material.

## Output for the production Pillar B consumer

Save one UTF-8 JSON object to exactly this path: ${output_path_json}
Use exactly this schema, with the actual report date, every executed query
copied verbatim from above, and all qualifying articles:
{"schema_version":"pillar-b-discovery.v1","report_date":"${report_date}","searches":[{"query":"exact executed query","status":"completed"}],"articles":[{"title":"original title","url":"https://publisher/article","source":"publisher name","summary":"source-backed search excerpt","published_date":"YYYY-MM-DD","date_evidence":{"url":"https://publisher/article","text":"verbatim publication-date evidence from this page or its source-backed search result"}}]}

The publication date must be exact and supported by the retained date evidence.
The evidence URL must identify the article itself. Do not substitute an event
date, a search execution date, or a website-wide copyright date.
Never borrow a date from another article or landing page, even from the same
publisher. Before saving, check every article/evidence URL pair. If its own
publication date cannot be verified, exclude that article rather than filling
its date from a related page.

The summary field is a factual search excerpt for that URL, not the final report
summary. Leave it empty when no usable excerpt exists. Do not generate taxonomy,
keywords, an executive summary or a finished report here: the per-URL authoring
step handles those.

Complete every required search query successfully before writing the output.
An empty articles array is valid ONLY when every required search succeeded but
no article qualified; retain the completed searches in the envelope.
A tool error, missing dependency, authentication failure or timeout is NOT a
zero-result search. If a required query cannot be completed, report the failed
query and blocker, and leave the output file absent or unchanged. Do not create
or overwrite it with an empty result or report completion after a tool failure.
Do not send messages, publish, or change URL state.
