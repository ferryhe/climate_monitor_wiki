# Meeting and article information contract

This contract implements the owner's 2026-10-04 requirements. Meetings and
ordinary information have separate collection, storage, verification and reader
views. The accepted IAA CSC PDF layout remains the presentation contract.

## Meeting fields and PDF mapping

| Canonical field | Web collection | PDF extraction | Reader / PDF mapping |
| --- | --- | --- | --- |
| `name` | Full event name from the event page | Full EVENT cell | Event |
| `event_type` | Meeting, conference, summit, webinar, deadline or retrospective | Preserve the original calendar `kind`; classification must retain its basis | Meeting filters; Event when material |
| `organizer` | Named organizer / host | HOST cell (`publisher` in the legacy intake) | Host |
| `status` | Scheduled, tentative, postponed, cancelled or retrospective | Only when explicitly stated; otherwise unknown | Event; controls current / future eligibility |
| `raw_date` / `date_evidence` | Original date wording and its quoted evidence | DATE(S) cell | Date(s) |
| `date_precision` | Day, month, quarter, year or unknown | Precision of DATE(S) | Date(s); never add a day to a month-only entry |
| `start_date`, `end_date` | Dates supported by the event wording | Parsed DATE(S), preserving interval endpoints | Date(s); date filtering and expiry |
| `raw_time_text` | Explicit clock time or interval | Explicit time in the PDF, when present | Date(s) |
| `event_timezone` | Explicit source timezone / offset | Explicit timezone in the PDF, when present | Date(s); no timezone inferred from the host country |
| `location` | Venue / city / country or explicit online format | Explicit location in the EVENT cell or accompanying wording | Event |
| `online_url` | Meeting / registration link, when provided | Explicit meeting / registration link | Event |
| `deadline_type`, `deadline_date` | Registration, consultation or expert review deadline | Explicit deadline row / wording | Separate key-date row; never replace meeting start / end |
| `relevance_reason` | Evidence-grounded climate / actuarial relevance | RELEVANCE cell (`relevance` in the legacy intake) | Relevance |
| `source_urls` | Event-detail source URL(s) | Links associated with this calendar row | Full URLs; one PDF import marker when applicable |
| `source_evidence` | Exact quoted excerpts and saved web body identity | Original row, document SHA, page and row identity | Kept for audit; detail view |

Clock times, timezones and locations absent from the PDF remain empty. Website
enrichment records its own source. It must not be attributed to the PDF.
Relevance written by the PDF author remains attributed analysis, even when the
meeting's factual fields have been checked against the website.

## Provenance, access and verification

These are separate properties. A PDF origin does not disappear after verification.

| Property | Meaning |
| --- | --- |
| Origin | Web collection or PDF import, with immutable source identity |
| URL access | Unchecked, accessible, unavailable or failed; retain final URL and check time |
| Content verification | Unchecked, verified, partial or conflict; retain supported fields and evidence |
| Collection eligibility | A verified PDF item participates in the same collected view as a web item |

Reader categories are PDF pending verification, URL accessible, and verified /
collected. Failure and conflict remain visible in the relevant information view.
Opening a homepage or reaching a login / challenge page does not verify an event.
An accessible URL alone does not promote a PDF record.

For a meeting, compare event identity, date interval and precision, organizer,
status, and any stated clock, timezone, venue or deadline. Each comparison must
be supported by evidence from the bound website content. Missing evidence is
partial verification. Contradictory facts are a conflict. Verification never
silently replaces the original PDF row.

A URL can describe multiple meetings or have several PDF summaries. Fetches may
be reused, but verification belongs to the specific record and source revision.
Revised content requires another verification. Matching only a URL is insufficient
for deduplication or promotion.

## Independent paths

| Stage | Meetings / calendar | Ordinary information |
| --- | --- | --- |
| Discovery and extraction | Dedicated event candidates and event-field extraction | Article candidates and article-field extraction |
| Storage | Event records, source observations, versions and event checks | Article records, source observations, versions and article checks |
| Date meaning | Event interval / deadline, independently of publication date | Article publication date, independently of report coverage |
| Verification | Event-field comparisons | Title, publication date and material summary facts |
| Reader view | Meeting / key-date list and its own filters and statuses | Project / article list and its own filters and statuses |
| PDF | Date(s), Event, Host, Relevance | Publisher, title, publication date, topic, full summary / caveat, URL |

Separate tables and processing state may share the existing SQLite database and
governed web reader. A failed article extraction must not prevent meeting
collection, and a failed meeting check must not block article import. A calendar
row must not become an article merely because it contains a URL.

The current meeting reader and range-report query explicitly include independent
deadlines. A future deadline remains eligible when its parent event has ended;
both dates retain their own meaning. The shared event-query library's default
is unchanged. Merging a collected event with a verified PDF observation retains
the PDF's original DATE(S) wording as display metadata and its immutable source row.
The checker and readers reuse the collector's identity resolver for supported
reschedules. Runtime PDF overlays resolve against the frozen public event history,
so a moved event retains its existing ID across HTML, Chat, PDF and the meeting list.

The public Meetings tab is independent of Historical Reports. It refreshes against
today in UTC and omits expired records, while the database retains their history.
The public reader uses the same UTC cutoff even when the host is configured for
New York time. Explicit `base_date` API queries remain available for historical
inspection; the current tab does not send one.

## Standalone per-record checker

`scripts/check_information.py` runs independently of acquisition and PDF intake.
It uses the existing governed `web_listening` reader and `TYPESAFE_API_KEY` for
field comparisons. Missing reader/model configuration is recorded as unavailable
or partial; it cannot produce a verified result.

```bash
python scripts/check_information.py --kind meetings --database /data/registry.sqlite3 --backup-dir /data/backups --limit 10 --result /data/meeting-check.json
python scripts/check_information.py --kind articles --database /data/registry.sqlite3 --backup-dir /data/backups --result /data/article-check.json
python scripts/check_information.py --kind meetings --database /data/registry.sqlite3 --backup-dir /data/backups --resume <run_id>
python scripts/check_information.py --kind meetings --database /data/registry.sqlite3 --backup-dir /data/backups --retry <run_id>
```

`--occurrence-id` selects individual observations. `--resume` skips saved terminal
attempts and continues the exact frozen input. `--retry` creates a new run for
unverified entries and preserves old attempts. Exit 0 means all targets verified;
exit 2 means there is pending work or unresolved evidence. JSON progress and the
summary identify each remaining record. Meetings and articles use independent
run and attempt tables; one does not block the other.

`--refresh-chat --queue-dir ... --runtime-wiki-dir ...` publishes a fresh immutable
snapshot for already activated PDF observations and reloads Chat using the existing
writer's reload configuration. It does not activate pending PDF batches or alter
previously generated reports/PDFs. A future scheduler can call the same commands.
No recurring job is installed by this module.

PDF calendar-only imports activate their own observation IDs and searchable
meeting page. They do not require artificial article observations. Verified web
packets participate in the collected meeting view without creating artificial
article acquisition rows; native event IDs and PDF source-local IDs stay distinct.

## Acceptance evidence

- Each original calendar row maps to one calendar source observation. Reader
  deduplication preserves source provenance and distinguishes events sharing URLs.
- Verified imports and collected observations resolve to one event reader record;
  differing or conflicting observations remain reviewable.
- All four PDF calendar columns match their corresponding stored fields. Current
  / future filtering accounts for the complete original date interval.
- Article summaries and caveats remain in project sections. Meeting names, hosts
  and event dates remain in the calendar section.
- The 2026-09-28 reference has 49 calendar rows. On 2026-10-04, 41 are current or
  future and 8 are expired. These counts are evidence for this reference only.
