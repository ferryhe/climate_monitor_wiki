# Climate Web Listening Monitor Implementation Plan

> **Historical planning snapshot:** Automation and cadence details below record
> the original design and are not current operating instructions. See
> `docs/weekly-cadence.md` for the Hermes rolling-PR workflow. The GitHub
> report-generator workflow planned below has been deleted.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an adapter-first monitor that uses `web_listening` to track supranational-organization sites, filters climate and actuarial signals, writes daily English source reports, syncs the wiki, and prepares GitHub automation for recurring updates.

**Architecture:** Keep the monitor orchestration in this repository and treat `web_listening` as an external acquisition dependency. Configuration and durable state live under `monitoring/`; Python orchestration lives in `climate_monitor/`; the existing `sources/ -> wiki/` sync remains the downstream contract. `ai_interface` stays out of v1 and can consume the artifacts later as a control/inspection surface.

**Tech Stack:** Python 3.12, `pydantic`, `PyYAML`, optional OpenAI Responses API web search/structured outputs, `web-listening` CLI or package installed from `ferryhe/web_listening`, existing `scripts/sync_source_wiki.py`, GitHub Actions.

---

## Scope And PR Split

One PR is enough for v1 if it includes:

- checked-in monitor configuration generated from the Excel source list;
- a testable local orchestrator with dry-run fixtures;
- optional live `web_listening` execution through a narrow adapter;
- optional OpenAI-backed research search and AI summaries when `OPENAI_API_KEY` is configured;
- daily source report writing and wiki sync;
- a GitHub Actions workflow that can run manually and on a schedule, then open a PR when files change.

Do not include these in the v1 PR:

- `ai_interface` runtime execution or UI wiring;
- reviewed per-site staged tree scopes for every site;
- browser/Playwright acquisition;
- deployed API reload after merge.

Those are follow-up PRs after the first scheduled run is proven.

## File Structure

- Create `monitoring/supranational_sources.yaml`: reviewed source registry converted from the Excel attachment.
- Create `monitoring/run_config.yaml`: climate/actuarial keywords, search queries, output policy, and web-listening settings.
- Create `monitoring/fixtures/web_listening_manifest_sample.json`: deterministic fixture for tests.
- Create `monitoring/fixtures/research_results_sample.json`: deterministic fixture for tests.
- Create `climate_monitor/__init__.py`: package marker.
- Create `climate_monitor/models.py`: typed data models for sources, candidates, summaries, and run results.
- Create `climate_monitor/config.py`: YAML loading and validation.
- Create `climate_monitor/dedupe.py`: URL/title/hash dedupe utilities.
- Create `climate_monitor/web_listening_adapter.py`: narrow wrapper around `web_listening` crawling/diff primitives plus dry-run fixtures.
- Create `climate_monitor/research_search.py`: OpenAI web-search-backed research lane plus fixture/offline lane.
- Create `climate_monitor/ai_filter.py`: deterministic keyword filter and optional OpenAI structured enrichment.
- Create `climate_monitor/report_writer.py`: Markdown source report renderer.
- Create `climate_monitor/orchestrator.py`: end-to-end run coordinator.
- Create `scripts/run_climate_monitor.py`: CLI entry point.
- Create `tests/test_climate_monitor_config.py`: source registry tests.
- Create `tests/test_climate_monitor_dedupe.py`: dedupe tests.
- Create `tests/test_climate_monitor_report_writer.py`: report format tests.
- Create `tests/test_climate_monitor_orchestrator.py`: dry-run end-to-end tests.
- Modify `requirements.txt`: add `PyYAML>=6.0.0`.
- Modify `.gitignore`: ignore generated monitor state and external checkouts.
- Modify `README.md` and `docs/source-update-sop.md`: document the recurring monitor.
- Create `.github/workflows/climate-monitor.yml`: scheduled/manual automation.

## Task 1: Add Monitor Configuration And Validation

**Files:**
- Create: `monitoring/supranational_sources.yaml`
- Create: `monitoring/run_config.yaml`
- Create: `climate_monitor/__init__.py`
- Create: `climate_monitor/models.py`
- Create: `climate_monitor/config.py`
- Create: `tests/test_climate_monitor_config.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Add dependency**

Add this line to `requirements.txt`:

```text
PyYAML>=6.0.0
```

- [ ] **Step 2: Create the source registry**

Create `monitoring/supranational_sources.yaml` with this structure. Include all 34 rows with a website URL from the Excel attachment; keep the 3 missing-URL organizations in `notes` only, not active sources.

```yaml
schema_version: supranational-sources.v1
generated_from: IAA CSC - Supranational Organizations Working Group - Database.xlsx
generated_at: 2026-05-14
missing_url_notes:
  - abbreviation: A2ii
    full_name: Access to Insurance Initiative
    note: Missing website link in source spreadsheet.
  - abbreviation: FAO
    full_name: Food and Agriculture Organization of the United Nations
    note: Missing website link in source spreadsheet.
  - abbreviation: UN Water
    full_name: UN-Water
    note: Missing website link in source spreadsheet.
sources:
  - key: iais
    abbreviation: IAIS
    full_name: International Association of Insurance Supervisors
    url: https://www.iais.org/
    high_priority: true
    tags: [insurance, supervision, climate]
  - key: iea
    abbreviation: IEA
    full_name: International Energy Agency
    url: https://iea.org
    high_priority: true
    tags: [energy, climate]
  - key: ipcc
    abbreviation: IPCC
    full_name: Intergovernmental Panel on Climate Change
    url: https://www.ipcc.ch/
    high_priority: true
    tags: [science, climate]
  - key: irff
    abbreviation: IRFF
    full_name: Insurance and Risk Finance Facility
    url: https://irff.undp.org/
    high_priority: true
    tags: [insurance, risk-finance]
  - key: issa
    abbreviation: ISSA
    full_name: International Social Security Association
    url: https://www.issa.int/
    high_priority: true
    tags: [social-security]
  - key: issb
    abbreviation: ISSB
    full_name: International Sustainability Standards Board
    url: https://www.ifrs.org/groups/international-sustainability-standards-board/
    high_priority: true
    tags: [disclosure, sustainability]
  - key: oecd
    abbreviation: OECD
    full_name: Organisation for Economic Co-operation and Development
    url: https://www.oecd.org/
    high_priority: true
    tags: [policy, climate]
  - key: pcaf
    abbreviation: PCAF
    full_name: Partnership for Carbon Accounting Financials
    url: https://carbonaccountingfinancials.com/
    high_priority: true
    tags: [carbon-accounting, finance]
  - key: psi
    abbreviation: PSI
    full_name: Principles for Sustainable Insurance
    url: https://www.unepfi.org/insurance/insurance/
    high_priority: true
    tags: [insurance, sustainability]
  - key: tnfd
    abbreviation: TNFD
    full_name: Taskforce on Nature-related Financial Disclosures
    url: https://tnfd.global/
    high_priority: true
    tags: [nature, disclosure]
  - key: undp
    abbreviation: UNDP
    full_name: United Nations Development Programme
    url: https://www.undp.org
    high_priority: true
    tags: [development, climate]
  - key: unep
    abbreviation: UNEP
    full_name: United Nations Environment Programme
    url: https://www.unep.org
    high_priority: true
    tags: [environment, climate]
  - key: wef
    abbreviation: WEF
    full_name: World Economic Forum
    url: https://www.weforum.org
    high_priority: true
    tags: [economy, risk]
  - key: world-bank
    abbreviation: World Bank
    full_name: World Bank Group
    url: https://www.worldbank.org/
    high_priority: true
    tags: [development, finance]
  - key: adb
    abbreviation: ADB
    full_name: Asian Development Bank
    url: https://www.adb.org/
    high_priority: false
    tags: [development, finance]
  - key: afdb
    abbreviation: AFDB
    full_name: African Development Bank Group
    url: https://www.afdb.org/en
    high_priority: false
    tags: [development, finance]
  - key: bcbs
    abbreviation: BCBS
    full_name: The Basel Committee on Banking Supervision
    url: https://www.bis.org/bcbs/index.htm
    high_priority: false
    tags: [banking, supervision]
  - key: bis
    abbreviation: BIS
    full_name: Bank for International Settlements
    url: https://www.bis.org/index.htm
    high_priority: false
    tags: [finance, supervision]
  - key: caf
    abbreviation: CAF
    full_name: CAF - Development Bank of Latin America and the Caribbean
    url: https://www.caf.com/en/
    high_priority: false
    tags: [development, finance]
  - key: fit
    abbreviation: FIT
    full_name: Forum for Insurance Transition to Net Zero
    url: https://www.unepfi.org/forum-for-insurance-transition-to-net-zero/
    high_priority: false
    tags: [insurance, transition]
  - key: fsb
    abbreviation: FSB
    full_name: Financial Stability Board
    url: https://www.fsb.org/
    high_priority: false
    tags: [finance, supervision]
  - key: g20
    abbreviation: G20
    full_name: Group of Twenty
    url: https://g20.org
    high_priority: false
    tags: [policy]
  - key: gca
    abbreviation: GCA
    full_name: Global Center on Adaptation
    url: https://gca.org/
    high_priority: false
    tags: [adaptation, climate]
  - key: ifac
    abbreviation: IFAC
    full_name: International Federation of Accountants
    url: https://www.ifac.org/
    high_priority: false
    tags: [accounting, disclosure]
  - key: ilo
    abbreviation: ILO
    full_name: International Labour Organization
    url: https://www.ilo.org/
    high_priority: false
    tags: [labor, social]
  - key: imf
    abbreviation: IMF
    full_name: International Monetary Fund
    url: https://www.imf.org/
    high_priority: false
    tags: [finance, macroeconomics]
  - key: ngfs
    abbreviation: NGFS
    full_name: Network for Greening the Financial System
    url: https://www.ngfs.net/
    high_priority: false
    tags: [finance, climate]
  - key: sif
    abbreviation: SIF
    full_name: Sustainable Insurance Forum
    url: https://sustainableinsuranceforum.org/
    high_priority: false
    tags: [insurance, supervision]
  - key: unctad
    abbreviation: UNCTAD
    full_name: United Nations Conference on Trade and Development
    url: https://unctad.org/
    high_priority: false
    tags: [trade, development]
  - key: unfccc
    abbreviation: UNFCCC
    full_name: United Nations Framework Convention on Climate Change
    url: https://unfccc.int/
    high_priority: false
    tags: [climate, policy]
  - key: who
    abbreviation: WHO
    full_name: World Health Organization
    url: https://www.who.int/
    high_priority: false
    tags: [health, climate]
  - key: wmo
    abbreviation: WMO
    full_name: World Meteorological Organization
    url: https://wmo.int/
    high_priority: false
    tags: [weather, climate]
  - key: wri
    abbreviation: WRI
    full_name: World Resources Institute
    url: https://www.wri.org/
    high_priority: false
    tags: [climate, research]
  - key: wto
    abbreviation: WTO
    full_name: World Trade Organization
    url: https://www.wto.org/
    high_priority: false
    tags: [trade]
```

The implemented file must contain exactly 34 active `sources` entries and 3 `missing_url_notes` entries.

- [ ] **Step 3: Create the run configuration**

Create `monitoring/run_config.yaml`:

```yaml
schema_version: climate-monitor-run-config.v1
report_title: Daily Climate & Actuarial Monitor
default_language: en
max_items_per_report: 12
website_lane:
  enabled: true
  default_fetch_mode: http
  high_priority_limit: 15
  normal_priority_limit: 10
research_lane:
  enabled: true
  lookback_days: 30
  max_results: 12
  queries:
    - climate risk insurance report
    - climate change actuarial research
    - natural catastrophe insurance climate report
    - climate financial risk supervision insurance
climate_keywords:
  - climate
  - warming
  - decarbonization
  - transition risk
  - physical risk
  - adaptation
  - resilience
  - net zero
  - emissions
  - natural catastrophe
  - flood
  - wildfire
  - heatwave
actuarial_keywords:
  - actuarial
  - actuary
  - insurance
  - reinsurance
  - solvency
  - capital
  - reserving
  - pricing
  - underwriting
  - catastrophe model
  - mortality
  - pension
  - supervision
  - disclosure
dedupe:
  url_tracking_path: monitoring/state/seen_urls.json
  title_tracking_path: monitoring/state/seen_titles.json
output:
  source_dir: sources
  wiki_dir: wiki
  write_empty_report: false
web_listening:
  project_path_env: WEB_LISTENING_PROJECT_PATH
  data_dir: .tmp/web_listening
  command: web-listening
```

- [ ] **Step 4: Write typed models**

Create `climate_monitor/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal


@dataclass(frozen=True)
class MonitorSource:
    key: str
    abbreviation: str
    full_name: str
    url: str
    high_priority: bool = False
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunConfig:
    report_title: str
    climate_keywords: tuple[str, ...]
    actuarial_keywords: tuple[str, ...]
    research_queries: tuple[str, ...]
    research_lookback_days: int
    max_items_per_report: int
    source_dir: str
    wiki_dir: str
    write_empty_report: bool


@dataclass(frozen=True)
class CandidateItem:
    title: str
    url: str
    summary: str
    source_name: str
    lane: Literal["website", "research"]
    published: str = ""
    detected_at: str = ""
    content_hash: str = ""
    climate_related: bool = False
    actuarial_related: bool = False
    relevance_reason: str = ""
    topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class MonitorRunResult:
    report_date: date
    report_path: str | None
    items: tuple[CandidateItem, ...] = ()
    dedup_notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    synced: bool = False
```

- [ ] **Step 5: Write config loader**

Create `climate_monitor/config.py`:

```python
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

from .models import MonitorSource, RunConfig


def _load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML object.")
    return payload


def _normalize_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Source URL is required.")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid source URL: {value}")
    return raw


def load_sources(path: str | Path) -> list[MonitorSource]:
    payload = _load_yaml(Path(path))
    sources = payload.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("sources must be a list.")
    result: list[MonitorSource] = []
    seen_keys: set[str] = set()
    for item in sources:
        if not isinstance(item, dict):
            raise ValueError("Each source must be a YAML object.")
        key = str(item.get("key", "")).strip().lower()
        if not key:
            raise ValueError("Each source needs a key.")
        if key in seen_keys:
            raise ValueError(f"Duplicate source key: {key}")
        seen_keys.add(key)
        tags = tuple(str(tag).strip() for tag in item.get("tags", []) if str(tag).strip())
        result.append(
            MonitorSource(
                key=key,
                abbreviation=str(item.get("abbreviation", "")).strip(),
                full_name=str(item.get("full_name", "")).strip(),
                url=_normalize_url(str(item.get("url", ""))),
                high_priority=bool(item.get("high_priority", False)),
                tags=tags,
            )
        )
    return result


def load_run_config(path: str | Path) -> RunConfig:
    payload = _load_yaml(Path(path))
    research = payload.get("research_lane", {}) or {}
    output = payload.get("output", {}) or {}
    return RunConfig(
        report_title=str(payload.get("report_title", "Daily Climate & Actuarial Monitor")).strip(),
        climate_keywords=tuple(str(value).strip().lower() for value in payload.get("climate_keywords", []) if str(value).strip()),
        actuarial_keywords=tuple(str(value).strip().lower() for value in payload.get("actuarial_keywords", []) if str(value).strip()),
        research_queries=tuple(str(value).strip() for value in research.get("queries", []) if str(value).strip()),
        research_lookback_days=int(research.get("lookback_days", 30)),
        max_items_per_report=int(payload.get("max_items_per_report", 12)),
        source_dir=str(output.get("source_dir", "sources")),
        wiki_dir=str(output.get("wiki_dir", "wiki")),
        write_empty_report=bool(output.get("write_empty_report", False)),
    )
```

- [ ] **Step 6: Write failing config tests**

Create `tests/test_climate_monitor_config.py`:

```python
from textwrap import dedent

import pytest

from climate_monitor.config import load_run_config, load_sources


def test_load_sources_normalizes_urls_and_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        dedent(
            """
            sources:
              - key: iais
                abbreviation: IAIS
                full_name: International Association of Insurance Supervisors
                url: www.iais.org
                high_priority: true
                tags: [insurance, climate]
              - key: iais
                abbreviation: Duplicate
                full_name: Duplicate
                url: https://example.com
            """
        ).strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate source key"):
        load_sources(path)


def test_load_sources_returns_valid_sources(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        dedent(
            """
            sources:
              - key: iais
                abbreviation: IAIS
                full_name: International Association of Insurance Supervisors
                url: www.iais.org
                high_priority: true
                tags: [insurance, climate]
            """
        ).strip(),
        encoding="utf-8",
    )

    sources = load_sources(path)

    assert len(sources) == 1
    assert sources[0].url == "https://www.iais.org"
    assert sources[0].high_priority is True
    assert sources[0].tags == ("insurance", "climate")


def test_load_run_config_reads_keywords_and_output_paths(tmp_path):
    path = tmp_path / "run_config.yaml"
    path.write_text(
        dedent(
            """
            report_title: Daily Climate & Actuarial Monitor
            max_items_per_report: 7
            climate_keywords: [Climate, Flood]
            actuarial_keywords: [Insurance, Reserving]
            research_lane:
              lookback_days: 30
              queries: [climate insurance report]
            output:
              source_dir: sources
              wiki_dir: wiki
              write_empty_report: false
            """
        ).strip(),
        encoding="utf-8",
    )

    config = load_run_config(path)

    assert config.report_title == "Daily Climate & Actuarial Monitor"
    assert config.max_items_per_report == 7
    assert config.climate_keywords == ("climate", "flood")
    assert config.actuarial_keywords == ("insurance", "reserving")
    assert config.research_queries == ("climate insurance report",)
    assert config.source_dir == "sources"
    assert config.write_empty_report is False
```

- [ ] **Step 7: Run config tests and make them pass**

Run:

```powershell
python -m pytest tests/test_climate_monitor_config.py -q
```

Expected: `3 passed`.

## Task 2: Add Dedupe And Report Rendering

**Files:**
- Create: `climate_monitor/dedupe.py`
- Create: `climate_monitor/report_writer.py`
- Create: `tests/test_climate_monitor_dedupe.py`
- Create: `tests/test_climate_monitor_report_writer.py`

- [ ] **Step 1: Write dedupe tests**

Create `tests/test_climate_monitor_dedupe.py`:

```python
from climate_monitor.dedupe import dedupe_items
from climate_monitor.models import CandidateItem


def _item(title: str, url: str) -> CandidateItem:
    return CandidateItem(
        title=title,
        url=url,
        summary="summary",
        source_name="Example",
        lane="research",
        climate_related=True,
        actuarial_related=True,
    )


def test_dedupe_items_normalizes_tracking_urls_and_titles():
    items = [
        _item("Climate risk report", "https://example.com/report?utm_source=x"),
        _item("Climate risk report ", "https://example.com/report"),
        _item("Different report", "https://example.com/other"),
    ]

    kept, notes = dedupe_items(items, seen_urls=set(), seen_titles=set())

    assert [item.title for item in kept] == ["Climate risk report", "Different report"]
    assert any("duplicate title" in note for note in notes)
```

- [ ] **Step 2: Implement dedupe**

Create `climate_monitor/dedupe.py`:

```python
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .models import CandidateItem

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def canonical_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_KEYS and not key.lower().startswith(TRACKING_PREFIXES)
    ]
    normalized_path = parsed.path.rstrip("/") or parsed.path
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), normalized_path, "", urlencode(query), ""))


def canonical_title(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "").strip().casefold())


def dedupe_items(
    items: list[CandidateItem],
    *,
    seen_urls: set[str],
    seen_titles: set[str],
) -> tuple[list[CandidateItem], list[str]]:
    kept: list[CandidateItem] = []
    notes: list[str] = []
    local_urls: set[str] = set()
    local_titles: set[str] = set()
    for item in items:
        url_key = canonical_url(item.url)
        title_key = canonical_title(item.title)
        if url_key in seen_urls or url_key in local_urls:
            notes.append(f"{item.title} ({url_key}) already in URL history - skipped")
            continue
        if title_key and (title_key in seen_titles or title_key in local_titles):
            notes.append(f"{item.title} duplicate title - skipped")
            continue
        kept.append(item)
        local_urls.add(url_key)
        if title_key:
            local_titles.add(title_key)
    return kept, notes
```

- [ ] **Step 3: Write report writer tests**

Create `tests/test_climate_monitor_report_writer.py`:

```python
from datetime import date

from climate_monitor.models import CandidateItem
from climate_monitor.report_writer import render_report


def test_render_report_matches_existing_source_shape():
    item = CandidateItem(
        title="Climate solvency report",
        url="https://example.com/report.pdf",
        summary="A concise English summary.",
        source_name="IAIS",
        lane="website",
        published="2026-05-01",
        climate_related=True,
        actuarial_related=True,
        topics=("solvency", "climate risk"),
    )

    text = render_report(
        report_date=date(2026, 5, 14),
        title="Daily Climate & Actuarial Monitor",
        items=[item],
        dedup_notes=["Older duplicate skipped"],
        sites_monitored=34,
        warnings=[],
    )

    assert text.startswith("# Daily Climate & Actuarial Monitor")
    assert "**Report Date:** 2026-05-14" in text
    assert "## Executive Summary" in text
    assert "## Website Updates" in text
    assert "**Title:** Climate solvency report" in text
    assert "**URL:** https://example.com/report.pdf" in text
    assert "## Dedup Notes" in text
    assert "- Sites monitored: 34" in text
```

- [ ] **Step 4: Implement report writer**

Create `climate_monitor/report_writer.py`:

```python
from __future__ import annotations

from datetime import date

from .models import CandidateItem


def _section_name(lane: str) -> str:
    return "Website Updates" if lane == "website" else "New Research"


def _render_item(index: int, item: CandidateItem) -> str:
    published = item.published or item.detected_at or "Unknown"
    topics = ", ".join(item.topics) if item.topics else "climate risk"
    actuarial = "Yes" if item.actuarial_related else "No"
    return "\n".join(
        [
            f"### {index}. {item.title}",
            f"**Title:** {item.title}  ",
            f"**Source:** {item.source_name}  ",
            f"**Summary:** {item.summary}  ",
            f"**URL:** {item.url}  ",
            f"**Published:** {published}  ",
            f"**Actuarial relevance:** {actuarial}  ",
            f"**Topics:** {topics}",
            "",
            "---",
        ]
    )


def render_report(
    *,
    report_date: date,
    title: str,
    items: list[CandidateItem],
    dedup_notes: list[str],
    sites_monitored: int,
    warnings: list[str],
) -> str:
    website_items = [item for item in items if item.lane == "website"]
    research_items = [item for item in items if item.lane == "research"]
    theme_terms = sorted({topic for item in items for topic in item.topics})
    theme_text = ", ".join(theme_terms[:8]) if theme_terms else "climate risk and actuarial monitoring"
    summary = (
        f"This report captures {len(items)} climate-related item(s) from monitored websites "
        f"and recent research search. Key themes: {theme_text}."
    )

    lines = [
        f"# {title}",
        f"**Report Date:** {report_date.isoformat()}",
        "",
        "## Executive Summary",
        summary,
        "",
    ]
    for lane_items, section in ((website_items, "Website Updates"), (research_items, "New Research")):
        if not lane_items:
            continue
        lines.extend([f"## {section}", ""])
        for index, item in enumerate(lane_items, start=1):
            lines.append(_render_item(index, item))
    if dedup_notes:
        lines.extend(["", "## Dedup Notes"])
        lines.extend(f"- {note}" for note in dedup_notes)
    if warnings:
        lines.extend(["", "## Warnings"])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend(
        [
            "",
            "## Summary",
            f"- Sites monitored: {sites_monitored}",
            f"- New items today: {len(items)}",
            f"- Key themes: {theme_text}",
            "",
        ]
    )
    return "\n".join(lines)
```

- [ ] **Step 5: Run tests**

Run:

```powershell
python -m pytest tests/test_climate_monitor_dedupe.py tests/test_climate_monitor_report_writer.py -q
```

Expected: `2 passed`.

## Task 3: Add Web Listening Adapter And Research Search

**Files:**
- Create: `monitoring/fixtures/web_listening_manifest_sample.json`
- Create: `monitoring/fixtures/research_results_sample.json`
- Create: `climate_monitor/web_listening_adapter.py`
- Create: `climate_monitor/research_search.py`
- Create: `climate_monitor/ai_filter.py`
- Create: `tests/test_climate_monitor_orchestrator.py`

- [ ] **Step 1: Add web-listening fixture**

Create `monitoring/fixtures/web_listening_manifest_sample.json`:

```json
{
  "schema_version": "web-listening-manifest.v1",
  "source": {
    "source_id": "iais",
    "site_url": "https://www.iais.org/",
    "site_name": "IAIS"
  },
  "discovered_items": [
    {
      "item_id": "iais-climate-supervision",
      "item_type": "page",
      "url": "https://www.iais.org/news/climate-risk-supervision-2026",
      "title": "Climate risk supervision update",
      "status": "new",
      "observed_at": "2026-05-14T10:00:00Z"
    }
  ],
  "downloaded_assets": []
}
```

- [ ] **Step 2: Add research fixture**

Create `monitoring/fixtures/research_results_sample.json`:

```json
[
  {
    "title": "Climate risk and insurance capital report",
    "url": "https://example.org/climate-insurance-capital-2026",
    "summary": "A recent report on climate risk, insurance capital, and supervision.",
    "source_name": "Example Research",
    "published": "2026-05-01"
  }
]
```

- [ ] **Step 3: Implement keyword and optional AI filter**

Create `climate_monitor/ai_filter.py`:

```python
from __future__ import annotations

from .models import CandidateItem, RunConfig


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(keyword.casefold() in lowered for keyword in keywords)


def classify_candidate(item: CandidateItem, config: RunConfig) -> CandidateItem:
    text = " ".join([item.title, item.summary, item.source_name])
    climate = _contains_any(text, config.climate_keywords)
    actuarial = _contains_any(text, config.actuarial_keywords)
    topics = tuple(
        keyword
        for keyword in (*config.climate_keywords, *config.actuarial_keywords)
        if keyword.casefold() in text.casefold()
    )
    return CandidateItem(
        title=item.title,
        url=item.url,
        summary=item.summary,
        source_name=item.source_name,
        lane=item.lane,
        published=item.published,
        detected_at=item.detected_at,
        content_hash=item.content_hash,
        climate_related=climate,
        actuarial_related=actuarial,
        relevance_reason="Matched configured climate/actuarial keywords." if climate else "No configured climate keyword matched.",
        topics=topics,
    )
```

Do not call OpenAI from this file in v1 tests. Add AI enrichment later in the orchestrator behind `OPENAI_API_KEY` if needed.

- [ ] **Step 4: Implement web-listening live adapter and manifest reader**

Create `climate_monitor/web_listening_adapter.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from .models import CandidateItem, MonitorSource


def _state_path(state_dir: Path, source: MonitorSource) -> Path:
    return state_dir / f"{source.key}.json"


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"content_hash": "", "links": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _title_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return url
    return path.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").strip() or url


def read_manifest_items(path: str | Path) -> list[CandidateItem]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    source = payload.get("source", {}) or {}
    source_name = str(source.get("site_name") or source.get("source_id") or "Unknown source")
    items: list[CandidateItem] = []
    for raw in payload.get("discovered_items", []) or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("status", "")).lower() not in {"new", "changed"}:
            continue
        title = str(raw.get("title") or raw.get("url") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not title or not url:
            continue
        items.append(
            CandidateItem(
                title=title,
                url=url,
                summary=f"{source_name} published or changed: {title}.",
                source_name=source_name,
                lane="website",
                detected_at=str(raw.get("observed_at", "")),
            )
        )
    return items


def collect_source_items(
    *,
    source: MonitorSource,
    state_dir: Path,
    fetch_mode: str = "http",
) -> list[CandidateItem]:
    try:
        from web_listening.blocks.crawler import Crawler
        from web_listening.blocks.diff import compute_hash, find_document_links, find_new_links, select_compare_text
    except Exception as exc:
        raise RuntimeError(
            "web_listening is required for live website monitoring. "
            "Install it with `python -m pip install ./external/web_listening` in CI "
            "or `pip install -e C:/Project/web_listening` locally."
        ) from exc

    state_file = _state_path(state_dir, source)
    previous = _load_state(state_file)
    with Crawler(fetch_mode=fetch_mode) as crawler:
        page = crawler.fetch_page(source.url, fetch_mode=fetch_mode)
    compare_text = select_compare_text(
        fit_markdown=page.fit_markdown,
        markdown=page.markdown,
        content_text=page.content_text,
    )
    content_hash = compute_hash(compare_text)
    current_links = list(page.metadata_json.get("links", []))
    if not current_links:
        from web_listening.blocks.diff import extract_links

        current_links = extract_links(page.raw_html, page.final_url or source.url)

    new_links = find_new_links(previous.get("links", []), current_links)
    doc_links = find_document_links(new_links)
    items: list[CandidateItem] = []
    if previous.get("content_hash") and previous.get("content_hash") != content_hash:
        items.append(
            CandidateItem(
                title=f"{source.abbreviation} website content changed",
                url=page.final_url or source.url,
                summary=f"{source.full_name} homepage or monitored landing page changed.",
                source_name=source.abbreviation,
                lane="website",
                content_hash=content_hash,
            )
        )
    for link in doc_links + [link for link in new_links if link not in doc_links]:
        items.append(
            CandidateItem(
                title=_title_from_url(link),
                url=link,
                summary=f"{source.abbreviation} added a new link observed from {source.url}.",
                source_name=source.abbreviation,
                lane="website",
            )
        )
    _save_state(state_file, {"content_hash": content_hash, "links": current_links})
    return items


def collect_website_items(
    sources: list[MonitorSource],
    *,
    state_dir: Path,
    manifest_fixture_path: str | Path | None = None,
) -> tuple[list[CandidateItem], list[str]]:
    if manifest_fixture_path:
        return read_manifest_items(manifest_fixture_path), []
    items: list[CandidateItem] = []
    warnings: list[str] = []
    for source in sources:
        try:
            items.extend(collect_source_items(source=source, state_dir=state_dir))
        except Exception as exc:
            warnings.append(f"{source.key}: {exc}")
    return items, warnings
```

This v1 path uses `web_listening` directly for live HTTP acquisition while keeping staged tree scope review as a follow-up.

- [ ] **Step 5: Implement research search**

Create `climate_monitor/research_search.py`:

```python
from __future__ import annotations

import json
import os
from pathlib import Path

from .models import CandidateItem, RunConfig


def read_research_fixture(path: str | Path) -> list[CandidateItem]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items: list[CandidateItem] = []
    for raw in payload:
        items.append(
            CandidateItem(
                title=str(raw["title"]),
                url=str(raw["url"]),
                summary=str(raw["summary"]),
                source_name=str(raw.get("source_name", "Research search")),
                lane="research",
                published=str(raw.get("published", "")),
            )
        )
    return items


def search_recent_research(config: RunConfig, *, fixture_path: str | Path | None = None) -> list[CandidateItem]:
    if fixture_path:
        return read_research_fixture(fixture_path)
    if not os.getenv("OPENAI_API_KEY"):
        return []
    # Live OpenAI web search is added in the GitHub workflow task behind a separate focused test.
    return []
```

- [ ] **Step 6: Run adapter tests through orchestrator dry-run**

The orchestrator test is added in Task 4. Run the current focused tests first:

```powershell
python -m pytest tests/test_climate_monitor_config.py tests/test_climate_monitor_dedupe.py tests/test_climate_monitor_report_writer.py -q
```

Expected: all existing focused monitor tests pass.

## Task 4: Add Orchestrator And CLI

**Files:**
- Create: `climate_monitor/orchestrator.py`
- Create: `scripts/run_climate_monitor.py`
- Modify: `tests/test_climate_monitor_orchestrator.py`

- [ ] **Step 1: Write end-to-end dry-run test**

Create `tests/test_climate_monitor_orchestrator.py`:

```python
from datetime import date
from textwrap import dedent

from climate_monitor.orchestrator import run_monitor


def test_run_monitor_writes_source_report_and_syncs_wiki(tmp_path):
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    manifest = tmp_path / "manifest.json"
    research = tmp_path / "research.json"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "index.md").write_text("# Wiki Index\n", encoding="utf-8")

    source_config.write_text(
        dedent(
            """
            sources:
              - key: iais
                abbreviation: IAIS
                full_name: International Association of Insurance Supervisors
                url: https://www.iais.org/
                high_priority: true
                tags: [insurance, climate]
            """
        ).strip(),
        encoding="utf-8",
    )
    run_config.write_text(
        f"""
report_title: Daily Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate, flood, wildfire]
actuarial_keywords: [insurance, supervision, capital]
research_lane:
  lookback_days: 30
  queries: [climate insurance report]
output:
  source_dir: {source_dir.as_posix()}
  wiki_dir: {wiki_dir.as_posix()}
  write_empty_report: false
""".strip(),
        encoding="utf-8",
    )
    manifest.write_text(
        """
{
  "schema_version": "web-listening-manifest.v1",
  "source": {"source_id": "iais", "site_name": "IAIS"},
  "discovered_items": [
    {
      "item_id": "1",
      "item_type": "page",
      "url": "https://www.iais.org/climate-supervision",
      "title": "Climate supervision update",
      "status": "new",
      "observed_at": "2026-05-14T00:00:00Z"
    }
  ],
  "downloaded_assets": []
}
""".strip(),
        encoding="utf-8",
    )
    research.write_text(
        """
[
  {
    "title": "Climate risk and insurance capital report",
    "url": "https://example.org/report",
    "summary": "A report about climate risk and insurance capital.",
    "source_name": "Example Research",
    "published": "2026-05-01"
  }
]
""".strip(),
        encoding="utf-8",
    )

    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 14),
        manifest_fixture_path=manifest,
        research_fixture_path=research,
        state_dir=tmp_path / "state",
        sync=True,
    )

    assert result.report_path is not None
    report_text = (source_dir / "climate-monitor-2026-05-14.md").read_text(encoding="utf-8")
    assert "Climate supervision update" in report_text
    assert "Climate risk and insurance capital report" in report_text
    wiki_text = (wiki_dir / "climate-monitor-2026-05-14.md").read_text(encoding="utf-8")
    assert "Source: [[sources/climate-monitor-2026-05-14]]" in wiki_text
    assert result.synced is True
```

- [ ] **Step 2: Implement orchestrator**

Create `climate_monitor/orchestrator.py`:

```python
from __future__ import annotations

from datetime import date
from pathlib import Path

from scripts.sync_source_wiki import sync_source_wiki

from .ai_filter import classify_candidate
from .config import load_run_config, load_sources
from .dedupe import dedupe_items
from .models import CandidateItem, MonitorRunResult
from .report_writer import render_report
from .research_search import search_recent_research
from .web_listening_adapter import collect_website_items


def _source_file_path(source_dir: Path, report_date: date) -> Path:
    return source_dir / f"climate-monitor-{report_date.isoformat()}.md"


def run_monitor(
    *,
    source_config_path: str | Path = "monitoring/supranational_sources.yaml",
    run_config_path: str | Path = "monitoring/run_config.yaml",
    report_date: date | None = None,
    manifest_fixture_path: str | Path | None = None,
    research_fixture_path: str | Path | None = None,
    state_dir: str | Path = "monitoring/state",
    sync: bool = True,
) -> MonitorRunResult:
    day = report_date or date.today()
    sources = load_sources(source_config_path)
    config = load_run_config(run_config_path)
    raw_items: list[CandidateItem] = []
    warnings: list[str] = []

    website_items, website_warnings = collect_website_items(
        sources,
        state_dir=Path(state_dir),
        manifest_fixture_path=manifest_fixture_path,
    )
    raw_items.extend(website_items)
    warnings.extend(website_warnings)

    raw_items.extend(search_recent_research(config, fixture_path=research_fixture_path))

    classified = [classify_candidate(item, config) for item in raw_items]
    relevant = [item for item in classified if item.climate_related]
    kept, dedup_notes = dedupe_items(relevant, seen_urls=set(), seen_titles=set())
    kept = kept[: config.max_items_per_report]

    source_dir = Path(config.source_dir)
    wiki_dir = Path(config.wiki_dir)
    if not kept and not config.write_empty_report:
        return MonitorRunResult(report_date=day, report_path=None, warnings=tuple(warnings), synced=False)

    source_dir.mkdir(parents=True, exist_ok=True)
    output_path = _source_file_path(source_dir, day)
    output_path.write_text(
        render_report(
            report_date=day,
            title=config.report_title,
            items=kept,
            dedup_notes=dedup_notes,
            sites_monitored=len(sources),
            warnings=warnings,
        ),
        encoding="utf-8",
    )

    synced = False
    if sync:
        sync_source_wiki(source_dir=source_dir, wiki_dir=wiki_dir)
        synced = True

    return MonitorRunResult(
        report_date=day,
        report_path=str(output_path),
        items=tuple(kept),
        dedup_notes=tuple(dedup_notes),
        warnings=tuple(warnings),
        synced=synced,
    )
```

- [ ] **Step 3: Implement CLI wrapper**

Create `scripts/run_climate_monitor.py`:

```python
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from climate_monitor.orchestrator import run_monitor


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the climate web listening monitor.")
    parser.add_argument("--source-config", default="monitoring/supranational_sources.yaml")
    parser.add_argument("--run-config", default="monitoring/run_config.yaml")
    parser.add_argument("--date", default="")
    parser.add_argument("--manifest-fixture", default="")
    parser.add_argument("--research-fixture", default="")
    parser.add_argument("--state-dir", default="monitoring/state")
    parser.add_argument("--no-sync", action="store_true")
    args = parser.parse_args()

    report_date = date.fromisoformat(args.date) if args.date else None
    result = run_monitor(
        source_config_path=Path(args.source_config),
        run_config_path=Path(args.run_config),
        report_date=report_date,
        manifest_fixture_path=Path(args.manifest_fixture) if args.manifest_fixture else None,
        research_fixture_path=Path(args.research_fixture) if args.research_fixture else None,
        state_dir=Path(args.state_dir),
        sync=not args.no_sync,
    )
    if result.report_path:
        print(f"Report written: {result.report_path}")
    else:
        print("No climate-related updates found; no report written.")
    for warning in result.warnings:
        print(f"Warning: {warning}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run orchestrator test**

Run:

```powershell
python -m pytest tests/test_climate_monitor_orchestrator.py -q
```

Expected: `1 passed`.

## Task 5: Add Live OpenAI Research Search And Summary Enrichment

**Files:**
- Modify: `climate_monitor/research_search.py`
- Modify: `climate_monitor/ai_filter.py`
- Create: `tests/test_climate_monitor_research_search.py`

- [ ] **Step 1: Add test with fake OpenAI client**

Create `tests/test_climate_monitor_research_search.py`:

```python
from climate_monitor.models import RunConfig
from climate_monitor.research_search import parse_openai_research_payload


def test_parse_openai_research_payload_returns_candidate_items():
    payload = {
        "items": [
            {
                "title": "Climate risk capital report",
                "url": "https://example.org/capital",
                "summary": "Report on climate risk and insurance capital.",
                "source_name": "Example",
                "published": "2026-05-01"
            }
        ]
    }

    items = parse_openai_research_payload(payload)

    assert len(items) == 1
    assert items[0].lane == "research"
    assert items[0].title == "Climate risk capital report"
```

- [ ] **Step 2: Implement parser and guarded live search**

Modify `climate_monitor/research_search.py` to include:

```python
from pydantic import BaseModel


class ResearchSearchItem(BaseModel):
    title: str
    url: str
    summary: str
    source_name: str = "Research search"
    published: str = ""


class ResearchSearchPayload(BaseModel):
    items: list[ResearchSearchItem]


def parse_openai_research_payload(payload: dict) -> list[CandidateItem]:
    parsed = ResearchSearchPayload.model_validate(payload)
    return [
        CandidateItem(
            title=item.title,
            url=item.url,
            summary=item.summary,
            source_name=item.source_name,
            lane="research",
            published=item.published,
        )
        for item in parsed.items
    ]
```

Then update `search_recent_research` so the no-fixture, key-configured path calls:

```python
from openai import OpenAI

client = OpenAI()
response = client.responses.parse(
    model=os.getenv("CLIMATE_MONITOR_SEARCH_MODEL", "gpt-5.5"),
    tools=[{"type": "web_search"}],
    input=[
        {
            "role": "system",
            "content": (
                "Find climate-related research papers and institutional reports published in the last 30 days. "
                "Prefer insurance, actuarial, risk management, solvency, supervision, disclosure, and catastrophe modeling relevance. "
                "Return only source-backed results."
            ),
        },
        {"role": "user", "content": "\\n".join(config.research_queries)},
    ],
    text_format=ResearchSearchPayload,
)
return parse_openai_research_payload(response.output_parsed.model_dump())
```

This follows the OpenAI Responses API web-search and structured-output pattern. Keep this code behind `OPENAI_API_KEY`; local tests must not call the network.

- [ ] **Step 3: Run focused tests**

Run:

```powershell
python -m pytest tests/test_climate_monitor_research_search.py tests/test_climate_monitor_orchestrator.py -q
```

Expected: `2 passed`.

## Task 6: Add GitHub Actions Automation

**Files:**
- Create: `.github/workflows/climate-monitor.yml`
- Modify: `.gitignore`

- [ ] **Step 1: Update gitignore**

Append to `.gitignore`:

```gitignore
.tmp/
monitoring/state/
external/
```

- [ ] **Step 2: Add workflow**

Create `.github/workflows/climate-monitor.yml`:

```yaml
name: Climate Monitor

on:
  workflow_dispatch:
  schedule:
    - cron: "30 10 * * 1-5"

permissions:
  contents: write
  pull-requests: write

jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout climate monitor wiki
        uses: actions/checkout@v4

      - name: Checkout web_listening
        uses: actions/checkout@v4
        with:
          repository: ferryhe/web_listening
          path: external/web_listening

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version-file: .python-version

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -r requirements.txt
          python -m pip install ./external/web_listening

      - name: Run climate monitor
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          CLIMATE_MONITOR_SEARCH_MODEL: ${{ vars.CLIMATE_MONITOR_SEARCH_MODEL || 'gpt-5.5' }}
          WEB_LISTENING_PROJECT_PATH: ${{ github.workspace }}/external/web_listening
        run: python scripts/run_climate_monitor.py

      - name: Validate generated wiki
        run: |
          python -m pytest
          node --check showcase/app.js

      - name: Create pull request
        uses: peter-evans/create-pull-request@v6
        with:
          commit-message: "docs: add automated climate monitor update"
          title: "Automated climate monitor update"
          body: "Automated update from the climate monitor workflow."
          branch: codex/automated-climate-monitor
          delete-branch: true
          add-paths: |
            sources/
            wiki/
            README.md
```

If `ferryhe/web_listening` is private, add a checkout token later. Do not add that token in this PR.

- [ ] **Step 3: Validate workflow syntax locally by reading it**

Run:

```powershell
Get-Content .github\workflows\climate-monitor.yml
```

Expected: file exists and includes `workflow_dispatch`, `schedule`, `contents: write`, and `pull-requests: write`.

## Task 7: Documentation And Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/source-update-sop.md`

- [ ] **Step 1: Document the monitor**

Add this section to `README.md` after "When Sources Update":

```markdown
## Automated Climate Monitor

The scheduled monitor reads `monitoring/supranational_sources.yaml`, uses `web_listening` as the external acquisition layer, filters climate-related and actuarial-relevant items, writes `sources/climate-monitor-YYYY-MM-DD.md`, and regenerates the matching wiki pages with `scripts/sync_source_wiki.py`.

Local dry run:

```bash
python scripts/run_climate_monitor.py \
  --date 2026-05-14 \
  --manifest-fixture monitoring/fixtures/web_listening_manifest_sample.json \
  --research-fixture monitoring/fixtures/research_results_sample.json
```

The GitHub workflow lives at `.github/workflows/climate-monitor.yml`. It opens a pull request only when generated files change.
```

- [ ] **Step 2: Update SOP**

Add this note to `docs/source-update-sop.md` under "Standard Flow":

```markdown
For automated runs, `scripts/run_climate_monitor.py` performs steps 1 and 2 together: it writes the daily source report and calls `sync_source_wiki`. Manual source edits can continue to use the existing flow.
```

- [ ] **Step 3: Run full verification**

Run:

```powershell
python -m pytest
node --check showcase/app.js
python scripts/run_climate_monitor.py --date 2026-05-14 --manifest-fixture monitoring/fixtures/web_listening_manifest_sample.json --research-fixture monitoring/fixtures/research_results_sample.json
python scripts/sync_source_wiki.py
git diff --check
```

Expected:

- pytest passes;
- Node syntax check exits 0;
- dry-run writes `sources/climate-monitor-2026-05-14.md`;
- sync writes `wiki/climate-monitor-2026-05-14.md`;
- `git diff --check` reports no whitespace errors.

## Implementation Order With Subagents

Use subagent-driven development after this plan is approved.

1. Controller creates or switches to branch `codex/climate-web-listening-monitor`.
2. Worker 1 owns Task 1 only: config files, models, config loader, config tests.
3. Worker 2 owns Task 2 only: dedupe, report writer, focused tests.
4. Worker 3 owns Task 3 only: fixtures, live `web_listening` adapter, research fixture reader, keyword classifier.
5. Controller integrates Tasks 1-3 and runs focused tests.
6. Worker 4 owns Task 4 only: orchestrator and CLI.
7. Worker 5 owns Task 5 only: OpenAI live search path with tests that mock parsing only.
8. Worker 6 owns Task 6 only: workflow and ignore rules.
9. Controller owns Task 7 docs and full verification.
10. Controller performs final code review, then commit/PR.

Do not dispatch multiple workers to edit the same files. Every worker must be told the codebase is shared and they must not revert other edits.

## Self-Review

- Spec coverage: website monitoring, climate filter, research search, dedupe, English report, existing wiki sync, and GitHub automation are covered.
- PR scope: one PR is enough for v1 because `ai_interface`, per-site scope review, and browser acquisition are excluded.
- Placeholder scan: the source registry is fully expanded with all 34 URL-bearing Excel rows.
- Type consistency: `MonitorSource`, `RunConfig`, `CandidateItem`, and `MonitorRunResult` are introduced before use and reused consistently.
- Verification: focused tests are added per component, followed by the existing repo checks.
