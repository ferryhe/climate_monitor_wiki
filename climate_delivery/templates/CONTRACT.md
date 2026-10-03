# IAA CSC template v1

`Report`, `Update`, and `Citation` contain only frozen dataclasses, strings and
tuples. The renderer consumes these values without Registry/DB, network, LLM,
summary generation or mail. There are exactly two adapters in `adapters.py`.
The existing range HTML still consumes its saved snapshot; its executive prose
uses the same pure stored-summary helper as the range adapter.

| Model field | Range snapshot mapping | Weekly summary mapping |
| --- | --- | --- |
| kind / identity / hash | range / snapshot_id / snapshot_sha256 | weekly / report date + SHA / report.sha256 |
| title / edition / window | range dates / Range report / date_range + timezone | report.title / report.edition or Weekly report / report.window or report week with coverage unavailable |
| run date | created_at calendar date | report.run_date; unavailable if absent (never report date) |
| executive prose | saved executive_summary points, or stored article summaries for legacy snapshots; existing range count/exclusion labels | executive_summary copied verbatim |
| update title/body | articles title, summary, content; pdf_source_updates title/summary | highlights title/summary copied verbatim |
| institution/topic | publisher and categories; unrecorded publisher explicitly labelled | article_provenance[url].source and article_semantics[url].categories; unrecorded publisher explicitly labelled |
| article/content identity | article_id + content_version_id; PDF core_article_id/pdf_article_id + content_sha256 | optional article_provenance[url].article_id + content_version_id; content_hash shown separately as Content SHA-256; historical report_version_id separately labelled Report article version; unavailable if absent |
| publication / PDF coverage | publication_date; PDF-only publication unconfirmed + coverage_period | provenance publication_date and coverage_period; publication unconfirmed if absent |
| citations | frozen citations; a legacy empty citation list may use its frozen canonical_url; PDF filename/page retained, PDF update URL retained | optional provenance citations, otherwise highlight URL (required by existing summary contract) |
| Key Dates | meeting.records and pdf_calendar.records; each source status and query identity retained | optional key_dates; unavailable when absent |
| dates table | full start_date/end_date interval plus raw_date/raw_time_text and date_precision/precision; name; publisher/institution/organizer/source; relevance/actuarial_relevance/relevance_reason; source_filename/page/SHA, source_url/url and all sources[].source_url | same explicit fields in key_dates |
| statistics | no institution coverage inferred from article counts | report.sites checked/succeeded/failed verbatim; unknown values labelled unavailable; Pillar A/B update counts derived only from frozen highlights |
| coverage limitations | frozen meeting/calendar failure status retained | monitoring_notes copied verbatim |
| optional A/B/C/watch | coverage (institution/status/detail), route_corrections (source/detail), glossary (term/definition), cross_cutting_watch strings | same optional fields |

Optional blocks are omitted when no corresponding input exists. Optional table
cells are labelled `Not provided`. Updates require a nonblank title and at
least one URL or valid PDF filename/page citation. Core hashes and dates fail
explicitly. Invalid supplied optional tables fail; they are not silently dropped.
No article count is presented as institutional coverage. No failed acquisition
or unavailable calendar is presented as quiet. Summaries are not rewritten.

Key Dates retain normalized bounds, original date wording and supplied precision.
The same pure display mapping feeds HTML and PDF. Past/current/future markers
compare the entire interval with the frozen run date, using the existing meeting
contract's pure calendar-bound helper; month/quarter/year precision keeps its
full bounds. Unknown/raw-only dates have no guessed calendar marker.
An independently verified meeting deadline_date becomes a separate Key Dates
row labelled with deadline_type, alongside any event interval. Its known ISO
day is marked against the frozen run date even when event precision is unknown.
It retains the same supplied institution, relevance and citations. A standalone
deadline does not acquire an unavailable placeholder event row.

The v1 fixed disclaimer and purpose are original template copy. The wordmark
is typeset `IAA | CSC`, rather than an extracted sample image. The navy/gold
cover, section bars, tables, margins and hierarchy follow the visual reference;
no reference facts, statistics or page counts are reused.

## Render identity and history

The single configured default is `templates.TEMPLATE_ID/TEMPLATE_VERSION`.
`render_identity()` also contains the renderer and ReportLab versions. Range
PDF caches use that identity; old `range-report-v1/v2` URLs first reuse their
original file. A missing legacy PDF may be reconstructed from its saved input
using the current template, leaving an independent current render and copying
its bytes to the previously missing legacy location. Existing files never change.

Weekly canonical archives retain their existing directory, download name,
schema-v1 manifest and email state key. New manifests add optional `rendering`
metadata (no migration). A valid archived summary/PDF is reused verbatim on a
repeat run, including retries after default changes. Original render metadata
is preserved; absent metadata identifies a legacy artifact. A new source SHA
gets the current template. `ensure_weekly_report_pdf` can cache an independent
render at `renders/<summary SHA>/<render identity>/...pdf` without touching the
canonical archive or mail; this is a library helper, not a new user API.

## Fonts and portability

Versioned assets in `assets/v1` embed DejaVu Sans regular/bold, Noto Sans Symbols 2 and Noto Sans SC
regular (static weight 400 instance of the open-source Noto Sans SC 2.004
variable font). DejaVu's redistribution license and Noto's SIL OFL 1.1 accompany
the assets; `fonts.json` records SHA-256 and source. FontTools was used only
to prepare the static asset, not as a runtime dependency. Latin, Greek, common
math symbols and Chinese are rendered with fonts that contain each glyph.
An unsupported character fails with its Unicode code point instead of being
silently dropped or painted as a square. Fonts do not depend on host installs.
