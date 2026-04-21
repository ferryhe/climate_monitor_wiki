# UI Surfaces

## Overview

The local demo now has two complementary tabs:

- `Chat`: a minimal single-column conversation surface with inline evidence drawers inside assistant messages.
- `Obsidian`: a restored browsing workspace with `Graph View`, `Dataview`, and `Note Detail`.

Both tabs share one piece of state: `activeContextPath`.
The chat tab also shares one response-control state: `answerMode`, with `brief` and `detailed`.

## Chat Tab

The chat surface is intentionally sparse:

- one conversation column
- sticky composer at the bottom
- active note badge in the header
- answer-mode switch for `Brief` vs `Detailed`
- source evidence tucked into collapsible sections per assistant reply

This keeps the answering flow close to the `c-ross-2` style while using the darker Obsidian palette already associated with the wiki workspace.
`Detailed` mode is intended to surface richer evidence from `sources/` raw reports instead of relying only on curated wiki notes.

## Obsidian Tab

The Obsidian workspace restores the older browsing affordances:

- `Graph View`: force-directed graph with a `Notes / Keywords` mode switch
- `Dataview`: searchable table for page metadata, links, and daily-report status
- `Note Detail`: metadata plus raw Markdown preview for the selected note

The current page order is `Dataview + Note Detail` first, then `Graph View`.
`Notes` mode keeps the Obsidian-style note-link graph.
`Keywords` mode now renders a note-to-concept map built from a backend concept index derived from both curated wiki notes and `sources/` raw reports.

Selecting a note node, Dataview row, or evidence source sets the active note for chat retrieval.
Clicking a keyword node applies that concept as a Dataview filter so you can jump straight to the relevant note set.
For daily report notes, the `Source` link in the detail panel points to the corresponding `sources/*.md` file on GitHub's default branch instead of the local wiki summary note.

## Active Context Rules

- Selecting a note in the Obsidian tab activates it for chat.
- Clicking a `wiki/` source card in chat jumps to the Obsidian tab and activates that note.
- Clicking a `sources/` source card opens the raw report directly.
- Clearing context removes the retrieval priority boost.
- The Obsidian plugin continues to send the vault's active note path as `contextPath`.

## Frontend Data Contract

The web workspace depends on `GET /api/config` returning:

- `wiki.documents`
- `documents[].title`
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
- `wiki.source_documents`

Those fields are enough to build the graph, Dataview table, and active-note routing before individual Markdown files are fetched for preview.
