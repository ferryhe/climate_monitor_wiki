# IAA CSC reference template v10

The renderer uses the approved independent PDF sample: Liberation Sans body
9.5 pt / 14.25 pt leading, navy/gold cover and banners, a compact paginated
contents with destinations and page numbers, gold update numbers, four-column
calendar and source appendix blocks.
The cover logo is the image supplied in IAA_CSC_Climate_Report_20260928.pdf.
No reference report text, statistics or page counts are copied into reports.

## Inputs

`Report`, `Update`, and `Citation` are frozen dataclasses containing strings
and tuples. Only two adapters exist: saved range snapshots and validated
weekly summaries. Rendering performs no DB query, website fetch, model call,
mail delivery or source-document read. It never changes the supplied input.

Range identity validates the snapshot content SHA, dates and citations.
Selection retains the existing Registry query contract. Explicit day dates
with PDF text evidence also select imported updates, without requiring a core
Registry link. These dates are PDF-stated, not website-verified. PDF coverage
dates are not promoted to article publication dates. Report link duplicates
are collapsed only when URL, date, title and full summary agree.
The range executive page uses one frozen, provider-backed synthesis when a
provider is available. Its evidence references are checked against the selected
snapshot. Without a usable provider, it reports snapshot statistics and the
provider status. The raw stored-summary provenance remains unchanged. Weekly
executive prose, monitoring notes and highlight summaries remain source supplied.

Updates retain title, publisher, topic, source citations and caveats. The reader
shows one cleaned stored summary when present, with the captured body as fallback;
distinct source-verification summaries remain visible. Reader output removes
operational IDs, hashes, raw keywords and repeated categories. Publisher names
are normalized from configured and explicitly mapped official source URLs or
exact known aliases; a known URL owner takes precedence over an inherited report
heading. Unknown publisher names remain unchanged, and titles are never used to
infer publishers. Long paragraphs can split across pages. Full source URLs and
file/page citations are shown to readers. File/page locators, article IDs, content versions and hashes
remain in frozen snapshots and model metadata. Missing publisher and
publication date are labelled explicitly.
Distinct stored summaries for the same URL remain distinct updates.

## Calendar

DATE(S), EVENT, HOST and RELEVANCE are the four reader columns. Known day
entries are sorted first; broader month/quarter/year windows are separate.
Imported calendars display the original date wording beside recorded normalized bounds. Normalized bounds and
precision remain in the snapshot and drive current/future filtering and sort.
Other calendar sources retain raw date/time wording and uncertainty labels.
Markers compare the complete interval with the frozen run date.
Verified independent deadlines remain separate rows. No day or interval end is
guessed. Known day intervals intersecting the next 14 days have a gold rule.

Only explicit publisher/institution/organizer/source fields supply HOST; only
explicit relevance fields supply RELEVANCE. Missing fields say Not provided.
Import reads table column geometry and accepts fields only when all cell text
matches the retained raw row. Existing imports can recover fields from the
SHA-matched original PDF without rewriting history. Unmatched summaries remain
labelled source context; recovered rows use their four cells directly.
Calendar event names link to the first stored source URL; duplicate visible URL
lines are omitted. Supplied locations, distinct online links and file/page
citations appear alongside the corresponding calendar row. Repeated raw context
and query/debug fields are hidden. With no source citation,
the event name links to its supplied online URL. The frozen record retains all
original fields and source URLs.
Rows can split when longer than a page; table headers repeat.

Optional coverage, route corrections, glossary and cross-cutting watch blocks
appear only when supplied. Repeated calendar notes and citations are not emitted
as a separate appendix. An article count is not institutional coverage.
Unavailable acquisition or calendar data is never presented as quiet.

## Versions and archives

The default is iaa-csc v10, renderer reportlab-3. Calendar clocks, timezones and
venues appear only when supplied by the source. Cache identity also includes
ReportLab's version. A new template receives a new cache path. Existing PDFs
are never overwritten, including prior shared templates and range-report-v1/v2.
A missing legacy range PDF can use the current saved-snapshot render; a missing
older shared-template artifact reports unavailable rather than falsely claiming
that the previous renderer was run. New renders have an independent manifest.

Weekly archives keep their existing paths, schema-v1 manifest and mail state.
A valid existing archive is reused verbatim across template changes and retries.
New manifests record optional rendering metadata; legacy manifests stay valid.
No email is sent by the renderer or by independent render checks.

## Assets

assets/v3 embeds unmodified Liberation Sans 2.1.5 regular, bold, italic and
bold italic with its SIL OFL 1.1 license. assets.json records source and hashes.
assets/v1 retains licensed DejaVu, Noto Sans SC and Noto Sans Symbols fallback
fonts. Chinese, Greek and supported symbols use fonts containing the glyph.
The source PDF attachment decoration is represented as [attachment]. Cookie
consent markers are represented as [cookie]. Other unsupported glyphs fail
explicitly. No host-installed font is required.

Page totals use a deterministic multi-pass ReportLab build; internal links and
outline destinations are created by the actual page canvas on every pass.
