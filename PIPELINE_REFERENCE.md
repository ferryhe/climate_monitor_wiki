# Climate Monitor Pipeline — Reference

The program and artifact map for the modular monitor and article-first pipeline. See
[README.md](README.md#new-flow) for the flowchart and
[PIPELINE_CONFIG.md](PIPELINE_CONFIG.md) for editable prompts and scheduling.

See [Article-first architecture](docs/article-first-architecture.md) for the shared Registry-to-Wiki/RAG boundary, shared intake activation and independent report tasks and deferred IAA CSC template requirements. Registry article ingestion precedes reports; the post-deploy Registry slot associates published reports and checks exact identity rather than ingesting articles for the first time.

Weekly finalization writes `acquisition-report-YYYY-MM-DD.md` into the run's
`report-staging/` directory and returns `acquisition_report_path` in its JSON
result. This operator report contains source outcome counts, included article
counts by pillar, coverage warnings/failure reasons, deduplication notes, and the
public report SHA-256. It stays outside `sources/`, wiki ingestion, and delivery.
The public weekly Markdown retains the checked/succeeded/failed totals but omits
the Coverage Limitations section. Retrying finalization recreates the operator
report from the frozen run evidence; it does not require another acquisition.

## Implementation and deployment status

As of the 2026-09-08 SSH audit, the same-run prepare → serial URL authoring →
executive summary → finalize path is implemented and sandbox-tested. Production
still has 12 enabled legacy Step jobs. The four-slot wrappers are a deployment
target, not a live inventory. Issue #87 is owner-closed; historical handoffs that
say it must stay open do not override that decision.

The library entry `weekly_monitor.driver.run_weekly_monitor` validates a completed
response and delegates to the orchestrator. The production CLI owns preparation
and the serial model queue before invoking that library. These are layers of one
path, not competing report generators.

## Program and artifact map

| Stage | Owner / public entry | Input → output |
|---|---|---|
| Pillar A acquisition | Public `RuntimeService.explore_site` / `refresh_site` | Configured sites → stored upstream results/continuations plus matching climate `acquisition-batch-result.v2` and `web-listening-manifest.v1` bridge artifacts |
| Hermes site hints | `build_task_binding` → `.agents/skills/climate-site-*/SKILL.md` | Selected short guidance and hashes → immutable `site_skill_inventory`; Hermes receives navigation/search hints, while `web_listening` remains the only governed reader |
| Pillar B discovery | Hermes `web_search` / `web_extract`; `prompt_loader`, `pillar_b_discovery` | Explicit report date + editable template → validated `pillar-b-discovery.v1` envelope |
| Prepare | `scripts/run_climate_monitor.py --production-weekly --authoring-mode prepare` | Outcome + manifest + Pillar B → `bundle.json`, `combined.json`, `candidate_item_snapshot.json`, `article_evidence.json`, `stats.json`, `v2_authoring_request.json` in staging |
| Identity/merge | `article_candidate_contract.py`, `candidate_aggregation.py`, `dedupe.py` | URL-bearing records → one canonical identity, all origins, source metadata |
| Evidence | `article_content_adapter.build_article_evidence_artifact` | Public governed upstream result → content/ref, attempts, fetch status, hash and explicit snippet/none fallback |
| Optional title extraction | `article_title.extract_page_title` | Verified HTML → main/article H1, other H1, Open Graph or HTML title, preserving case |
| URL authoring | Existing CLI `--authoring-mode run` | One URL's frozen evidence → two booleans, summary, basis, evidence hash, categories, keywords; validated checkpoint in `url_authoring/` |
| Executive authoring | Same CLI after every URL completes | Qualified summaries → `executive_authoring.json`; no invocation for an empty qualified set |
| Finalize | Same CLI `--authoring-mode finalize`; `weekly_monitor.driver`, `orchestrator` | Prepared bundle + validated v2 response → report Markdown, semantic sidecar, candidates/evidence, URL history transaction |
| Delivery | `python -m climate_delivery.cli run` | Validated report → briefing, content-addressed PDF/manifest and retained email |
| Publication | `publish_weekly_reports.py`, `weekly_wiki_refresh.sh` | Unpublished reports → isolated clone, regenerated `sources/`/`wiki/`, rolling content PR |
| Deployment | `reload_and_smoke_test.py` and controlled deployment runbook | Reviewed/merged content and code → deployed corpus and verified API |
| Registry | `weekly_registry_refresh.py`, `climate_registry.weekly` | Exact deployed report + delivery identity → candidate sync, coverage checks, backup/promotion |
| Registry Wiki rendering | `climate_registry/wiki.py` | One consistent Registry snapshot → article pages and PDF source observations |
| Runtime knowledge activation | `scripts/run_pdf_intake_writer.py`, `climate_registry.pdf_pipeline` | Validated web/PDF intake manifest + pinned snapshots → fresh runtime overlay via the existing single writer, reload and active pointer |
| Date-range PDF | `scripts/generate_range_report.py`, `climate_registry.range_reports` | Explicit dates + consistent Public snapshot + validated active overlay → the same frozen inputs and renderer used by Chat |
| Web / retrieval | `api_server.py`, `agentic_wiki/`, `showcase/` | Published reports + active Registry projection → historical reports, article details, cited chat |

Module paths without a directory prefix are under `climate_monitor/`. Upstream
reader and tool-selection policy belongs to `web_listening`; climate does not implement a second
HTTP/browser/stealth crawler. A policy refusal remains a terminal acquisition
outcome. Available snippets are explicitly labelled, and a URL with no evidence
cannot acquire an invented summary.

The public URL-fetch workflow stores original source evidence and a cleaned
Markdown derivative. Climate verifies the selected derivative through the
Runtime artifact API and keeps both artifact identities and hashes. Historical
retained HTML remains byte-identical in the authoring view. The offline title
helper neither fetches nor calls a model. `--no-page-titles` disables it on a
fresh prepare.

## Run and resume

Provision the upstream artifacts and complete Pillar B search first. The monitor
consumes those files; `--print-pillar-b-prompt` only renders a task for Hermes.
Use one explicit Monday date and absolute external paths throughout a run:

```bash
python scripts/run_climate_monitor.py --production-weekly \
  --authoring-mode run --report-date "$REPORT_DATE" \
  --acquisition-batch "$CLIMATE_OUTCOME_ARTIFACT" \
  --web-listening-manifest "$CLIMATE_MANIFEST_ARTIFACT" \
  --pillar-b-artifact "$CLIMATE_PILLAR_B_ARTIFACT" \
  --staging-dir "$CLIMATE_STAGING_DIR" \
  --state-dir "$CLIMATE_STATE_DIR" --source-dir "$CLIMATE_SOURCE_DIR" \
  --wiki-dir "$CLIMATE_WIKI_DIR" \
  --source-config "$CLIMATE_SOURCE_CONFIG" --run-config "$CLIMATE_RUN_CONFIG" \
  --site-scopes "$CLIMATE_SITE_SCOPES" \
  --model "$MODEL" --model-provider "$MODEL_PROVIDER" --json
```

This is an executing command, not preflight. Sandbox runs must supply isolated
output/state paths. `--no-sync --no-update-seen-state` suppress wiki sync and URL
history promotion for monitor-only rehearsals; they do not disable network/model
calls. Explicit fixture runs require the dry-run controls below.

- Each URL has a fresh Hermes context, limited to its own evidence. Relevance
  rules are included in the same invocation as summary/categories/keywords.
- The application computes climate AND actuarial/insurance relevance. A false
  decision has empty summary fields. Finalize does not reapply the old keyword
  filter to validated v2 decisions.
- Each result is validated and atomically checkpointed. A failed URL is recorded
  and the queue continues; any unfinished URL blocks executive authoring and
  finalization. There is no article-count cap.
- Resume with the same command/staging. Completed results are revalidated and
  reused. Inputs, model/provider, prompt, taxonomy, title policy and destinations
  must still match; changed inputs require fresh staging.
- Prepare and finalize share history filtering and same-date carry-forward.
  Staging binds the effective candidate URL set and history destination; a
  history change that alters selection stops resume before model work. A
  completed run can replay its own same-date candidates, including rejected
  articles, without another model invocation. Pending report/history commits
  recover through the existing transaction before normal selection resumes.
- Executive authoring has its own checkpoint. Finalize reuses the prepared
  evidence and verifies date, URL set and hashes without recrawling.
  The executive response must be prose paragraphs; lists/headings are rejected
  before checkpoint completion so delivery cannot mistake findings for legacy
  monitoring bullets. Resume then repeats only executive authoring.
- Progress is stderr; `--json` stdout is one final result. Hermes runtime retry
  limitations and stdin compatibility are documented in PIPELINE_CONFIG.md.

## Identity, dates and report format

Pillar A/B are discovery origins, not separate namespaces. `source` is an
institution/website name; `pillar` carries A or B. Canonical URL merging keeps all
origins. Titles are never deduplication keys. URL history commits only with the
validated final report bundle; interrupted work cannot mark unfinished articles
as processed.

Site counts come from the upstream outcome, not the number of articles or the
configured source list. `total = updated + unchanged + blocked + failed +
unresolved`. The WRI sandbox input had **one** requested site and **one** unchanged
site; 164 discovery rows do not mean 164 checked sites. Never copy fixture counts
such as 57/42/15 into a live report.

For a full scan, the existing `--acquisition-batch` path can contain an array of
original public v2 scope outcomes, and `--web-listening-manifest` an array of their
original v1 exports. Keep a distinct upstream task identity for each requested
seed, even when several seeds belong to one organization. The driver delegates
count aggregation to the upstream public contract and checks each export's own
parent run, source, seed and artifact identity. Every successful scope needs
exactly one export; terminal failures need none. Do not replace these identities
with a fabricated common parent run. Single-scope object inputs remain supported.
The monitoring total counts requested entries, which can exceed the number of
organizations. `failed` and `unresolved` remain separate buckets. Empty Pillar A
discovery produces an empty candidate list, with no placeholder article.

Pillar B uses the three-calendar-month window ending on the explicit report date.
Production prepare and wrapper preflight require `pillar-b-discovery.v1`: the
matching report date, every current prompt query marked completed exactly once,
and articles with `title`, `url`, `source`, `summary`, `published_date` and
`date_evidence` (`url`, `text`). Missing, old or future publication dates and
evidence from a different article are rejected before staging. Date evidence is
a retained producer assertion; the validator does not infer or independently
prove a date from a URL. An empty result still requires all completed searches.
Historical four-field arrays remain readable by the legacy candidate adapter;
production prepare does not accept them. Candidate origins point to the original
envelope's `/articles/N` row and retain its SHA, including publication evidence.

Markdown retains the existing weekly contract: report identity and monitoring
counts, executive narrative, Pillar A/B sections grouped by primary category,
article titles, ordered categories, summaries, keywords and original links.
The semantic sidecar binds structured metadata to that report. Delivery reuses
the narrative executive summary; old bullet-only reports retain their existing
extractive fallback. Taxonomy is owned by
`monitoring/taxonomies/article_categories_v1.yaml`, not a duplicate label list in
a cron prompt. The legacy `daily` document type and library cadence default remain
load-bearing; weekly operation is explicit.

## Legacy scripts and cleanup

`step1_pillar_a.py`, `step2_save_state.py`, `step3_aggregate.py`, the Step 3b/filter
scripts, `step5_build_md.py`, `step6_render_pdf.py`,
`step7b_extract_conferences.py`, `step8_sync_registry.py` and
`step9_update_website.py` remain compatibility/rollback entrypoints. The last SSH
inventory still found legacy jobs enabled, so they cannot yet be described as
unused or deleted safely.

As of this writing (PR #145), `~/.hermes/cron/jobs.json` on the production
host has every job that calls one of these scripts by name set to
`"enabled": false`; the only currently-enabled jobs are the five
`issue124-*.sh` wrappers, which call `scripts/hermes_job.py` — a different,
fully env-var-driven entrypoint that never imports or shells out to any of
these legacy scripts. This does not contradict the SSH-inventory finding
above (a separate, dated audit); if a future inventory finds one of these
scripts' jobs re-enabled, treat that as new information superseding this
note, not as evidence this note was wrong.

Their env-var defaults (`CLIMATE_WIKI_HOME`, `CLIMATE_WL_REPO`,
`CLIMATE_WL_STATE`) resolve relative to this script's own repo checkout
(`Path(__file__).resolve().parents[1]`) and its conventional sibling
directories (`web_listening/` next to this repo) instead of a hardcoded
host path, so a manual rollback run works on any host that
follows the same sibling-directory layout without editing tracked files.
Set the env var explicitly when a host's layout differs.

`CLIMATE_ARTIFACT_ROOT`, `CLIMATE_REGISTRY_DB`, and
`CLIMATE_REGISTRY_BACKUP_DIR` are different: these are writable production
targets (rendered PDFs, the Registry database, its backups), not read-only
sources, so they have **no default at all** and must be set explicitly.
Guessing a sibling-directory default for a writable path let a script run
from a temporary clone or worktree silently initialize a brand-new, empty
Registry/artifact tree at a guessed location instead of failing — a
rollback command could appear to succeed while never touching the real
production state. See `tests/test_writable_paths_require_explicit_env.py`
for the regression coverage.

After the unique new schedule is verified, disable old scheduler callers, check
remaining imports/tests and remove obsolete code/configuration made redundant by
the replacement. Keep historical reports and required compatibility contracts.
Do not add a parallel driver, copied reader stack or duplicate prompt definition.
Superseded issue handoffs, closeout audits and completed migration plans are
available in Git history. Keep current instructions in these module/runbook
documents rather than retaining another active-looking copy.

## Verification and cutover

On 2026-09-28, the VM application image at `2aea05d` had a qualified, active
Playwright 1.0.0 in its existing Runtime volume. A fresh isolated volume was
also qualified against the controlled fixture. A fresh-state exploration of
all 36 sources then had 8 sources with all seeds successful, 9 partial,
16 blocked and 3 incomplete: 42/115 distinct attempted seeds succeeded and
77 candidate rows were returned. Playwright actually ran four times, all on
OECD seeds, but Cloudflare blocked those reads. IEA stopped at
`acquisition.auth_required` before browser dispatch. External aggregate:
`$HOME/.local/share/climate-monitor/site-browser-pilot/site-health-20260928T163303Z-a1a170/aggregate.json`,
SHA-256 `29d74dba010fcf5bad57afa118bdb52e0010589ab4532ca46df69a8e305a9e26`.
This is seed exploration, not article/Registry verification or scheduler proof.

A later IEA-only probe on 2026-09-28 found a bounded alternative: explicit
Playwright on an exact news article exhausted its 60-second request budget, but
an exact official PDF linked from the IEA report page on
`iea.blob.core.windows.net` fetched successfully after its URL was added as a
single-path scope exception. The article adapter had no PDF-to-Markdown
derivative; the existing `pypdf` dependency extracted the 62-page file for
inspection. No IEA report has been ingested or synced to Registry. The dated
details and hashes are in the `climate-site-iea` skill and the isolated VM
artifact directory.

A separate IPCC-only probe fetched the discovered `/assessment-report/ar7/`
page as 13,153 Markdown characters through `acquisition.web_http`; the page
contains AR7 planning milestones but no page publication date. Its body hash and
date caveat are recorded in the `climate-site-ipcc` skill. Registry remains
unverified for both sources.

An IRFF-only probe also fetched a discovered agricultural insurance risk-sharing
article as 7,485 Markdown characters through `acquisition.web_http`. Its body
states `POSTED 10 June, 2026`; see the `climate-site-irff` skill for the receipt,
content hash and summary. Registry remains unverified.

ISSA remains blocked: the root, the exact climate analysis page and the official
publications index each returned `robots.forbidden` in the governed Runtime.
Search-index listings are recorded only as unverified candidate leads in the
`climate-site-issa` skill; no content was ingested.

ISSB seed discovery remains incomplete, but the exact climate-taxonomy news
article and an ISSB monthly updates index/article were fetched through the
governed reader. The May 2026 update contains tentative nature-related decisions;
do not represent them as final requirements. The dated receipts and hashes are
in `climate-site-issb`; no ISSB content has been ingested or synced to Registry.

OECD's seed pages remain Cloudflare-blocked. The official newsroom search UI
offers an RSS route, but its query-bearing feed returned `web_http.url_redacted`
through the Runtime with no body or status; the browser tool was not dispatched.
Keep the source blocked and the API host out of persistent scope until the
governed reader can preserve that exact URL. Details and the pilot receipt are
in `climate-site-oecd`.

PCAF's resources and standard hubs are readable. Two current standard PDFs were
acquired as Runtime source artifacts and extracted with the shared `pypdf`
dependency; the article adapter has no PDF-to-Markdown derivative, and a single
collector batch hit its byte budget before producing candidates. Exact paths,
version dates and hashes are in `climate-site-pcaf`. No PCAF report was ingested
or synced to Registry.

PSI's queryless official news RSS was fetched through the governed reader (HTTP
200, 10 dated items). Its COP31 summit announcement and linked event page both
became Markdown through `acquisition.web_http` and
`transform.simple_html_markdown`. The general transform flattened the event
tables; a one-off standard-library parser over the verified Runtime HTML source
recovered both dated agendas. TypeSafe recommended only those two exact event
paths, now present in PSI scope. The collector still does not promote RSS items
as candidates, and nothing was ingested or synced to Registry. See
`climate-site-psi` for receipts and hashes.

TNFD's news index and exact news article remained `acquisition.interaction_required`
in the isolated reader even though the ordinary browser showed dated listings.
The scoped `/publication/` detail page returned 5,763 Markdown characters, and a
query-free exact linked PDF path returned HTTP 200. Shared `pypdf` extracted the
135-page September 2026 guidance. The ordinary collector did not promote the PDF
as a candidate, so no persistent PDF scope exception was added. See
`climate-site-tnfd`; no TNFD content was ingested or synced to Registry.

UNDP Climate Promise's three configured seeds succeeded in a fresh isolated run.
Two exact articles, including a 2026-09-28 story, were fetched as Markdown through
the governed Runtime with HTTP 200 and allowed robots decisions. Filtered
?ctype=blog listing URLs were redacted, but exact story paths worked. TypeSafe
recommended the shared reader and transform without a UNDP-specific script. The
dated evidence and hashes are in the climate-site-undp skill; meetings, report
generation and Registry handoff remain unverified.


UNEP's four configured entry points all succeeded in the isolated Runtime.
A fresh news-listing run selected only an Interactives page and a September 17
press release, while the browser showed a September 28 climate story. Its exact
URL returned HTTP 200 and 22,716 Markdown characters, but the common transform
also retained substantial navigation and footer text. TypeSafe recommended
recording the discovery gap and noise without adding scope or a site-specific
cleaner. Although the UNEP scope contains a browser hint, actual acquisition
receipts showed web_http; the common workflow documents that this hint does not
select Playwright. See the climate-site-unep skill for hashes; meetings, report
generation and Registry remain unverified.


WEF's three configured seeds returned HTTP 200 through web_http; a current
September 28 carbon-market article also fetched as 14,439 Markdown characters.
Its complete body is present, but the governed Markdown omits the target H1/date,
and the standard seed candidates did not include this browser-visible latest
story. The configured browser hint did not dispatch Playwright. TypeSafe
classified the title/date omission as a shared reader/artifact follow-up rather
than a WEF-specific script. See climate-site-wef for receipts and hashes;
meetings, report generation and Registry remain unverified.

WRI's four configured seeds succeeded, and one exact September 10 article
returned 30,741 Markdown characters with title, author, date and body. It has
roughly 2 KB of leading site navigation; TypeSafe recommended recording that
noise without a WRI-only cleaner. Meeting capture remains untested. See
`climate-site-wri` for the article hash.

WTO's configured seed run remained blocked, but the ordinary browser rendered
the climate overview, news list and exact September 25 CBAM dispute article. The
governed reader received HTTP 200 with `robots.allowed` for both the overview and
article, then failed `transform.simple_html_markdown` with `transform.html_invalid`
on each. TypeSafe recommended an upstream transform follow-up rather than a
WTO-specific parser; the regular meeting date in the article is visible in the
browser but no Markdown was produced. See `climate-site-wto` for receipts.

UN-Water's three configured entrypoints remain blocked by `robots.forbidden`,
and the normal browser showed a Cloudflare security verification page. TypeSafe
recommended stopping until the site offers a distinct documented public route
or changes its policy. Search indexing exposed an RSS query on the same blocked
`/news` path; it was not requested because the query does not change the robots
path decision. No bypass, scope change or content fetch was attempted.
See `climate-site-unwater` for the isolated-run digest.

CarbonPool's exact `/post/` article and archived webinar pages fetched as
Markdown, but the shared transform omitted their H1/date metadata. The browser
shows the webinar's article date separately from its July 22 event date. Two
temporary `/blog` discovery probes returned category pages but missed visible
post links. TypeSafe chose to canonicalize the exact webinar seed to `/post/`
and leave general discovery for an upstream collector follow-up; no
CarbonPool-only parser was added. See `climate-site-carbonpool` for hashes.

World Bank configured discovery remains blocked (0/4 seeds). A separate fresh
Runtime did fetch one exact official COP31 campaign page with HTTP 200 and
`robots.allowed`: 5,099 Markdown characters with the page title and 9–20 November
2026 date range; its event agenda still says “coming soon”. TypeSafe favored
recording that single-page success while leaving seed discovery unresolved
(confidence 0.47; choice probability 0.61). No scope was expanded; details and
hash are in climate-site-world-bank. Meetings, reporting and Registry remain
unverified.

ADB's configured three seeds and a bounded exact read of its official RSS page
were all rejected by `robots.forbidden`. Normal browser pages remain readable:
the climate topic links current stories and publications, and a representative
publication page exposes its September 2026 date, summary, DOI and PDF link.
TypeSafe selected a browser-only/manual disposition (confidence 1.0); no ADB
scope expansion or scraper is justified. The PDF text was not verified. See
climate-site-adb for exact observations; automated acquisition, reporting and
Registry remain blocked or unverified.

AfDB's configured four seeds and a fresh exact article read were rejected by
`robots.forbidden`. The normal browser can render the climate topic and current
news list, and a September 28 story exposes a readable article body. TypeSafe
selected browser-only/manual reading after the exact governed read was denied
(confidence 1.0); no scope expansion or scraper is justified. See
climate-site-afdb for the exact URL and evidence hash. Automated acquisition is
blocked; reporting and Registry remain unverified.

ISSB seed discovery remains incomplete, but the exact climate-taxonomy news
article and an ISSB monthly updates index/article were fetched through the
governed reader. The May 2026 update contains tentative nature-related decisions;
do not represent them as final requirements. The dated receipts and hashes are
in `climate-site-issb`; no ISSB content has been ingested or synced to Registry.

BCBS configured seed discovery remains blocked (0/2). The exact current climate
guideline HTML page fetched through the governed reader (2,956 Markdown
characters); its official PDF did not yield project Runtime text. TypeSafe chose
manual exact-page HTML plus a shared PDF-transform follow-up (confidence 1.0),
with no BCBS-only script or scope expansion. See `climate-site-bcbs` for hashes
and the distinction between browser PDF reading and Runtime acquisition.

BIS configured seed discovery remains blocked (0/4), but its global browser
search led to an exact Green Swan 2026 event page that the governed reader
fetched as Markdown (2,482 characters, dated October 19, 2026). Some agenda row
boundaries are flattened; TypeSafe classified this as partial success and a
shared transform quality issue (confidence 1.0). No BIS parser or scope change
was made; recurring discovery, structured meeting extraction, reporting and
Registry remain unverified. See `climate-site-bis` for the exact evidence.

CAF's configured four seeds and one exact September 23 resilience-housing
article all failed in the isolated Runtime with `gateway.tls_certificate_invalid`.
The normal browser displayed the dated article and full body. TypeSafe chose a
manual-browser status pending TLS diagnosis (confidence 1.0); TLS validation was
not weakened and no content was ingested. See `climate-site-caf` for the failed
Runtime receipt and hash. Automated acquisition and Registry remain blocked.

FIT has partial automated coverage: its news seed returned two candidates,
while the landing page required interaction and the publications seed attempted
an HTTPS downgrade. A verified June 22 implementation-guide article fetched as
7,760 Markdown characters. The public detail page links its PDF through a
third-party form requiring personal details; no form data was submitted and the
PDF remains unverified. TypeSafe recorded this as viable article access with a
gated report (confidence 1.0). See `climate-site-fit`; no custom script, scope
expansion or Registry sync was performed.

FSB has partial automated coverage: two of four seeds returned four candidate
rows, and an exact July 14, 2025 climate-roadmap report page yielded 2,284
Markdown characters with its title, date and summary. The page does not prove
full PDF acquisition; root scope and query-bearing press discovery remain
blocked. TypeSafe kept this as partial HTML success (confidence 1.0), with no
scope expansion or site script. See `climate-site-fsb`; Registry remains
unverified.

G20's configured homepage and feed, plus one exact official events-calendar
read, were all denied by `robots.forbidden` in the Runtime. Normal browser access
shows the 2026 calendar and media links, but no agendas; linked agency pages are
not first-party G20 content. TypeSafe kept G20 as browser-manual only (confidence
1.0); there was no bypass, scope change or ingestion. See `climate-site-g20`.

GCA has partial coverage: three of four seeds returned six candidates, and an
exact June 19, 2026 local-adaptation article fetched as 6,244 Markdown characters
with title, date and body. Its RSS seed remains rejected by scope; the article
has minor drop-cap/share-footer noise. TypeSafe kept this as partial HTML success
(confidence 0.99), with no script or scope change. See `climate-site-gca`; meetings
and Registry remain unverified.

IFAC has partial coverage: two of three seeds returned four candidates, and an
exact June 1, 2026 first-party climate-disclosure article fetched as 13,638
Markdown characters with title, date, authors and six implementation steps.
The `rss.xml` seed remains rejected by scope. TypeSafe found the article path
viable and the output clean (confidence 1.0); no script or scope change was made.
See `climate-site-ifac`; meetings and Registry remain unverified.

SIF has partial automated access: its two configured seeds succeeded, and a known exact resource HTML page and linked PDF were robots-allowed. Adding only the exact report path during an in-memory discovery probe still did not surface that report. Shared `pypdf` extraction is partial for the one-page infographic; TypeSafe recommends no custom parser and no scope change. See `climate-site-sif`; meeting extraction and Registry remain unverified.
UNCTAD is browser/manual only. Both configured seeds and an exact current climate article were denied by robots policy (robots endpoint HTTP 403), while the current climate topic, article and meeting detail page rendered in the browser. Its canonical climate topic path differs from the repo include path, and meeting paths are explicitly excluded; TypeSafe recommends keeping scope unchanged while Runtime access is blocked. See `climate-site-unctad`; no content was ingested and Registry was not run.

UNFCCC is browser/manual only. Current homepage and exact news pages render in a normal browser, but the Runtime news-index and exact-article probes both failed the shared HTML quality transform and returned no candidates. The exact September 21 speech is browser-readable, while its HTTP reader got 200/robots-allowed but produced no Markdown. TypeSafe recommends no scraper or scope change. The current scope also excludes `/calendar` and `/events`; no meeting extraction or Registry run occurred. See `climate-site-unfccc` for evidence hashes.
WHO has partial access: an exact August 5 climate-health news item is robots-allowed and produced 5,783 Markdown characters. A one-seed topic-page probe with a temporary `/news/item` path still returned no candidates due origin/path policy errors. TypeSafe recommends no persistent path/origin expansion; meetings and Registry remain unverified. See `climate-site-who` for evidence hashes.
WMO has partial automated coverage: two of three current seeds succeed, and the exact State of the Global Climate 2025 detail page returned 6,471 Markdown characters. The publication-series index is a 404; its current series page yielded only a News candidate, while the filtered “View all editions” URL was redacted before Runtime made a request. TypeSafe recommends using exact known report URLs without changing scope or adding a scraper. The Full Report PDF, meetings and Registry remain unverified. See `climate-site-wmo` for hashes.
NGFS is browser/manual only: all four configured seeds and one exact current prudential-supervision guide detail page were denied with `robots.forbidden` after its robots endpoint returned HTTP 403. The official publications directory still exposes the September 2026 guide and its PDF/slide download buttons in a normal browser. TypeSafe selected manual handling (confidence 1.0); no scope exception, scraper or file download was made. See `climate-site-ngfs`; meeting extraction, reporting and Registry remain unverified.
IMF is currently browser/manual only. Its four configured seeds returned `robots.forbidden`; a one-off exact July 10, 2026 Tanzania RSF article read was also denied after the IMF robots endpoint returned HTTP 403, with zero article bytes. The browser-visible article states that the RSF supports climate resilience and climate-finance reforms. TypeSafe recommended the bounded exact check (confidence 0.76), then selected manual-only handling (confidence 1.0). No scope expansion, custom scraper or ingestion is justified. See `climate-site-imf`; reporting and Registry remain unverified.
ILO now has verified partial access: a governed read of the canonical just-transition page succeeded and returned two publication candidates, and a separate exact read captured a current September 18, 2026 green-business article as 9,880 Markdown characters. Replacing the stale legacy seed with that canonical page is supported by an in-memory 200-success probe; a temporary `/resource/article` path addition returned no extra candidates, so it was not persisted. The RSS failure, full current seed coverage, meeting extraction and Registry remain unverified. TypeSafe kept ILO partial (confidence 1.0) and recommended the seed replacement (confidence 0.95). See `climate-site-ilo`; no source-specific script or ingestion was added.

Evidence through 2026-09-12:

- Full-range managed acquisition `20260912T120731-495c4dd2` ended with 15 source
  successes, eight policy rejections and 13 incomplete sources. Hermes emitted
  no budget precheck but consumed exactly the old 8/8 search calls and 40/40
  results after one five-result search for only eight sources, leaving 13 gaps
  without a first search opportunity. This proves the old default was too small;
  a later two-source canary also proved that the real public tool accepts a
  ten-result call, so five results cannot be treated as a deterministic per-call
  ceiling. New tasks now freeze `trusted-candidate-handles.v3`: Hermes selects
  public native-search candidates and calls the attempt-scoped stage/finalize
  tools under `acquisition-task-v2`, while the runner persists the complete real
  search ledger and governed article receipts and assembles Registry input from
  them. The v1 acquisition task remains unchanged for frozen legacy/v2 runs.
  Search planning and
  per-call result sizing remain provider-owned, with no application search-call,
  cumulative-result, per-call-result or token limit; actual counts remain
  evidence only. The 5,000-unit controlled fetch limit, per-item retry limit and
  cumulative runtime limit stay enforced before dispatch. Missing
  `agent_protocol` remains exact legacy behavior, explicit v2 remains frozen,
  and legacy/v2/v3 attempts cannot mix on resume. A fresh complete full-range v3
  run remains required.

- Runtime/discovery/handoff follow-up: full SSH sandbox suite **1791 passed /
  5 skipped** with the pinned upstream installed, and **1718 passed / 78 skipped**
  without it. Both runs retained the three existing warnings. The full-chain
  harness now calls the real monitor ledger producer and checks its report SHA.
- Historical September 2026 validation used web_listening `fd541f0`; that
  evidence is retained as the old-side comparison and is not current runtime
  proof. The deployment target is Python 3.12 with `web-listening` 0.1.0 pinned
  to `web_listening_new` revision `ac2343f89bc7939736d85f049ebe2beac571034a`.
  Fresh exact-20, container lifecycle
  and downstream rehearsal gates are required before production cutover.
- Live dated Pillar B discovery completed all four required tool queries. The
  validator rejected one article whose date evidence referred to another page;
  after verification and exclusion, five articles passed for June 7–September 7.
  This tests the discovery gate; it is not a complete configured-site canary.
- Delivery and status mounts are connected on the server. Update status returns
  200; job status reports `snapshot_unavailable` because the four-slot schedule
  has not run. Historical PDF backfill still skips August 31 (incomplete article
  artifacts) and September 3 (invalid canonical Markdown); sources are preserved.
- Reviewed runtime implementation before documentation pruning, with the pinned
  upstream installed: full SSH
  sandbox pytest **1769 passed / 5 skipped**, with three existing warnings.
  Compilation, shell/JavaScript syntax and whitespace checks passed. Independent
  code and Markdown reviews passed after history/resume and executive-delivery
  regressions were fixed.
- After removing four unused Markdown files and their obsolete text-only test,
  a separate clean environment verified `web_listening` was absent: full suite
  **1695 passed / 78 skipped**, with three existing warnings. Dependency
  consistency passed. Environment-dependent skips are not live acquisition proof.
- The single-site WRI-derived input exercised 163 unique URLs and produced a
  22-article PDF preview. It did not cover the full configured site list; its
  older Pillar B input was reused, so it is not a freshness proof.
- Fresh Hermes Pillar B search completed all four required queries and returned
  four candidates dated June 8, June 9, June 30 and July 9 for the June 7–September
  7 window. Manual review retained three; the IAIS market article only briefly
  mentioned climate. This was a discovery test, not a new whole-report run.
- A first search run failed because the sandbox dependency cache was read-only,
  yet wrote `[]` with exit 0. A writable sandbox cache fixed tool execution. The
  prompt now distinguishes tool failure from zero results. Production now also
  validates the completed-query and dated-article envelope before staging.

Before production cutover: deploy the reviewed dated-discovery and monitor-ledger
integration, use the verified Python 3.12/Hermes runtime, and run all configured
sites with real Pillar B discovery;
validate monitor → delivery dry-run → publisher no-push → Registry dry-run with
the same report identity. Then merge/deploy the reviewed code, switch to the unique
four-slot schedule, read back each command/timezone and observe a normal weekly
cycle. Only then retire old jobs and temporary worktrees, retaining a rollback
point. A passing fixture, owner-closed issue or scheduled reminder is not evidence
that this sequence has completed.

## Hermes job wrappers (AC-1/3/10)

The four `scripts/hermes_job_*.sh` wrappers resolve an absolute repository and
interpreter and invoke `scripts/hermes_job.py` from any working directory.
`--preflight` validates paths and contracts without dispatch or writes. All
runtime paths must be explicit, absolute and already provisioned. No wrapper
reads `.env`. Child output is suppressed to keep recipients and SMTP errors
out of scheduler logs.

The monitor requires `CLIMATE_OUTCOME_ARTIFACT`, `CLIMATE_MANIFEST_ARTIFACT`,
`CLIMATE_PILLAR_B_ARTIFACT` and `CLIMATE_STAGING_DIR`. The wrapper invokes the
existing CLI with `--authoring-mode run`: prepare, serial URL authoring,
executive authoring and finalize. It resolves model/provider through the existing
monitor configuration. Operators do not assemble unrelated response/evidence/stats
files. The previous `live_acquisition_contract_unavailable` placeholder has been
replaced by the executable same-run path; invalid or mismatched inputs still fail.

Fixtures require both `CLIMATE_DRY_RUN=1` and `CLIMATE_DRY_RUN_FIXTURE_DIR`.
Dry-run output/state paths must be within an explicit existing
`CLIMATE_DRY_RUN_ROOT` under `/tmp`. There is no automatic fixture fallback.
The monitor additionally requires existing `CLIMATE_STATE_DIR`,
`CLIMATE_SOURCE_DIR` and `CLIMATE_WIKI_DIR`, plus absolute existing
`CLIMATE_SOURCE_CONFIG`, `CLIMATE_RUN_CONFIG` (weekly report title), and
`CLIMATE_SITE_SCOPES` configuration files. Library daily defaults are unchanged.

Email uses `python -m climate_delivery.cli run --report PATH --output-dir PATH
--state-dir PATH --config PATH`, with absolute paths from `CLIMATE_REPORT_PATH`,
`CLIMATE_DELIVERY_OUTPUT_DIR`, `CLIMATE_DELIVERY_STATE_DIR` and
`CLIMATE_DELIVERY_CONFIG`. A completed same-date monitor slot and latest ledger
report SHA are required. `load_delivery_config` supplies exactly four recipients
and resolves required SMTP settings. The delivery pipeline validates the sidecar,
generates and validates its content-addressed PDF before SMTP dispatch.
The wrapper requests the CLI's JSON result, verifies its report date and SHA
against the committed Markdown/sidecar, and appends the monitor ledger attempt
before marking the scheduler slot completed. It requires an external existing
`CLIMATE_RUN_LEDGER_DIR`. Failed or malformed results cannot produce a success
identity. Dry runs validate the result without appending a production attempt.

Publisher requires `CLIMATE_REPORTS_DIR`, `CLIMATE_RUN_LEDGER_DIR`, and, for
production, `CLIMATE_PUBLISH_LOCK`. It validates the selected report before
invoking the absolute `scripts/publish_weekly_reports.py`. Its dry-run is the
existing pending-report validator's no-push plan, not publication.

Registry requires `CLIMATE_REGISTRY_ENABLE=1` and
`CLIMATE_HUMAN_MERGE_DEPLOY_VERIFIED=1`, plus `CLIMATE_EXPECTED_REPORT_SHA256`,
`CLIMATE_SOURCE_DIR`, `CLIMATE_REGISTRY_DB`, `CLIMATE_DELIVERY_OUTPUT_DIR`,
`CLIMATE_REGISTRY_BACKUP_DIR`, `CLIMATE_REGISTRY_LOCK`, and
`CLIMATE_RUN_LEDGER_DIR`. It invokes `scripts/weekly_registry_refresh.py`;
`CLIMATE_DRY_RUN=1` passes `--dry-run` and stops before capture, promotion or reload.
An eligible current-week dry-run records `registry_dry_run_exit_N` only in its
isolated status directory, with `not_dispatched`; pre-slot/historical rehearsals
return the code in stdout without falsifying a scheduled occurrence.
Production additionally requires `CLIMATE_REGISTRY_WRITE_ENABLE=1`, `API_BASE_URL`
and `SITE_HOST`. The existing runner verifies deployed corpus, publisher ledger,
artifact and DB identity. A blocked or dry-run Registry is never full completion.

Explicit wrapper calls require `REPORT_DATE` (Monday, ET business date); the
`--scheduled` guard resolves it from the anchored ET clock. All wrappers require an external
`CLIMATE_JOB_STATUS_DIR`. These snapshots are local-only evidence. Render has
no shared source and `/api/job-status` remains 503 `not_configured`; see
[the status contract](docs/job-status.md). The 2026-09-08 SSH audit found 12 enabled legacy Step jobs on the real server;
the four-slot target was not installed. The intended schedule is not a provisioning claim.

The current target is 08:00/09:00/10:00/10:30 ET every other Monday, anchored
to September 14, 2026. Use the DST-aware guarded checks in
[the ET runbook](docs/biweekly-et-deployment.md) and read back every real job.
