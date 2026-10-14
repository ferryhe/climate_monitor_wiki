# UI Surfaces

## Overview

The web application has three complementary operator tabs:

- `Historical Reports`: the default operational archive for weekly briefings,
  PDFs, source articles, and article-level provenance.
- `Chat`: a minimal single-column conversation surface with inline evidence drawers inside assistant messages.
- `Obsidian`: a restored browsing workspace with `Graph View`, `Dataview`, and `Note Detail`.

The three tabs share one piece of state: `activeContextPath`.
The chat tab also shares one response-control state: `answerMode`, with `brief`, `detailed`, and `executive`.

## Historical Reports Tab

The application opens on `Historical Reports`. Operators use it to validate the
published weekly output without reading server files:

- choose a report date from the newest-first list;
- read the narrative `Executive Summary` and `Monitoring Snapshot`;
- download the report PDF;
- inspect the articles that appeared in that report; and
- switch to `Article Database` to search and filter the full article history by
  publisher, pillar, or report date.

Selecting an article opens its metadata, captured content, classification, and
report appearances in the detail pane. If the optional Registry or delivery
artifact mount is unavailable, this tab reports that state without affecting
Chat or the Obsidian explorer.

For Article Detail metadata, a complete current-content Registry enrichment is
the primary atomic `summary`/`categories`/`keywords` bundle. The existing
per-article JSON annotations remain a compatibility fallback, including
reviewed source-annotation fields; the browser payload and ordering are
unchanged.

The Registry UI keeps `summary` and `report_summary` distinct because they have
different provenance: `summary` belongs to the current atomic semantic bundle,
while `report_summary` is the text observed in the historical report. All three
Registry surfaces resolve one display presentation in the same order:
`summary`, legacy `enrichment.summary`, then `report_summary`. When the last
fallback is used, the UI labels it `Historical report`; it does not infer the
missing categories or keywords.

## Chat Tab

The chat surface is intentionally sparse:

- one conversation column
- sticky composer at the bottom
- active note badge in the header
- answer-mode switch for `Brief`, `Detailed`, and `Report`
- source evidence tucked into collapsible sections per assistant reply

This keeps the answering flow close to the `c-ross-2` style while using the darker Obsidian palette already associated with the wiki workspace.
`Detailed` mode is intended to surface richer evidence from `sources/` raw reports instead of relying only on curated wiki notes.
`Report` mode is intended to produce sectioned report-style answers for period summaries such as `this month` or explicit date ranges, using theme clusters as the main organizing structure and day-by-day coverage as supporting context.

## Obsidian Tab

The Obsidian workspace restores the older browsing affordances:

- `Graph View`: precomputed graph payloads with a `Notes / Keywords` mode switch
- `Dataview`: searchable table for page metadata, links, and daily-report status
- `Note Detail`: metadata plus raw Markdown preview for the selected note

The current page order is `Dataview + Note Detail` first, then `Graph View`.
`Notes` mode shows the wiki's internal page relationships from both Obsidian
wikilinks and local Markdown `.md` links. Links to external websites, source
reports, and paths outside the wiki stay out of this graph. The Page Index
search scopes Notes mode. Notes and Concept Map use static radial layouts, with
page labels revealed on hover, keyboard focus, or selection; selecting a note
also highlights its linked pages and edges. `Expand graph` opens the current map
to the available screen area; Escape or `Close full-screen graph` returns to the
page. In the expanded view, selecting a note shows its title and reminds the
reader that closing the map returns to the full Note Detail pane.
`Keywords` mode now renders a note-to-concept map built from a backend concept index derived from both curated wiki notes and `sources/` raw reports.
The API now precomputes both graph payloads so the frontend can hydrate the Obsidian workspace directly instead of assembling the keyword graph on the fly.

Wiki pages include approved article content and PDF/source-observation pages
rendered from Registry evidence. The display title uses the Markdown page
heading; lowercase headings are made readable and show the canonical host when
one is present. A domain-only PDF observation heading is shown as a PDF source
observation with its explicit website and page number. The original Markdown,
page path, and retrieval content remain available in Note Detail. The Concept
Map hides exact structural headings and bare domain labels from the graph view
only; the full concept catalog, pages, and evidence remain available elsewhere.

### How the Keywords graph gets its concepts

The Keywords graph is an index over existing pages and source reports; it does
not write Wiki content. Its `CONCEPT_RULES` are 27 manually maintained pattern
rules in `agentic_wiki/wiki_agent.py`. The index also considers H2–H4 headings,
bold source-entry labels, and titles of topical Wiki pages. For each Wiki page,
it combines matches from that page with matches from its associated source
report when one is linked, then counts each concept once per Wiki page. The
graph shows up to 18 recurring concepts found on at least two pages; when there
are no such recurring concepts, it falls back to up to 12 indexed concepts.
Structural labels and bare domains are hidden from the graph presentation only.

This is separate from Registry article `summary`, `categories`, and `keywords`,
which describe an article's semantic enrichment, and from the `Category Map`,
which uses the Registry's article-category taxonomy. These indexes have
different inputs and purposes; a concept match does not create an article
annotation or a Wiki page.

### How Wiki pages are produced

The normal refresh projects retained reports and eligible Registry article and
PDF/source-observation records into derived Markdown pages, then refreshes the
Wiki index. Article pages retain available verified article content, Registry
summaries, and their provenance. PDF/source-observation pages retain the report-provided
summary, citation, and observation details, and mark unconfirmed article
details as unconfirmed. Existing topical Wiki pages are preserved by the sync;
the concept index does not synthesize or replace them.

Pages and the index carry their real internal links: Obsidian wikilinks and
local Markdown `.md` links feed Notes mode. The graph reflects those links, and
the Keywords mode reflects the concept index. Chat retrieval reads Wiki pages
and retained `sources/` reports independently of graph edges, so graph indexing
does not author or alter the facts used in answers.

Selecting a note node or Dataview row sets the active note for chat retrieval.
Clicking a keyword node applies that concept as a Dataview filter so you can jump straight to the relevant note set.
For daily report notes, the `Source` link in the detail panel points to the corresponding `sources/*.md` file on GitHub's default branch instead of the local wiki summary note.

## Active Context Rules

- Selecting a note in the Obsidian tab activates it for chat.
- `Use in chat` moves the selected note's display title into a persistent status
  beside the composer. Chat still uses the same question input, and retrieval
  prioritizes that note until the context is cleared or another note is chosen.
- Clicking an inline citation opens its matching evidence entry in that assistant reply.
- Clicking a meeting source card opens its matching Dates record in the app; article and report cards open their matching Historical Reports detail. The original source URL remains available from those details.
- Source cards show a retrieval score only when one was supplied, using its raw score without a percentage or probability label. After an answer arrives, the chat positions the current question at the top with the answer below it.
- Clearing context removes the retrieval priority boost.
- The Obsidian plugin continues to send the vault's active note path as `contextPath`.

## Frontend Data Contract

The web workspace depends on `GET /api/config` returning:

- `wiki.documents`
- `documents[].title`
- `documents[].display_title`
- `documents[].path`
- `documents[].type`
- `documents[].date`
- `documents[].words`
- `documents[].links`
- `documents[].concepts`
- `documents[].source_path`
- `documents[].source_url`
- `concepts[].label`
- `concepts[].type`
- `concepts[].document_count`
- `github_blob_base_url`
- `default_answer_mode`
- `answer_modes`
- `prompt_starters[].label`
- `prompt_starters[].prompt`
- `prompt_starters[].answer_mode`
- `graphs.notes.nodes[]`
- `graphs.notes.links[]`
- `graphs.keywords.nodes[]`
- `graphs.keywords.links[]`
- `graphs.keywords.static_layout`
- `wiki.source_documents`

Those fields are enough to hydrate the precomputed graphs, Dataview table, prompt starters, and active-note routing before individual Markdown files are fetched for preview.

The Historical Reports tab additionally consumes the read-only
`/api/registry/*` routes. Narrative briefings and PDF links are projected from
the optional `CLIMATE_DELIVERY_OUTPUT_DIR` artifact mount by the report-detail
endpoint; the browser never reads delivery state or recipient metadata.
