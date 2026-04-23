from textwrap import dedent

from scripts.sync_source_wiki import sync_source_wiki


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip("\n"), encoding="utf-8")


def test_sync_source_wiki_generates_daily_pages_and_rebuilds_index(tmp_path):
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"

    _write(
        source_dir / "climate-monitor-2026-04-21.md",
        """
        # Daily Climate Monitor
        **Report Date:** 2026-04-21

        ## Executive Summary
        April 21 summary with [linked evidence](https://example.com) and **strong** signal.
        """,
    )
    _write(
        source_dir / "climate-monitor-2026-04-23.md",
        """
        # Daily Climate Monitor
        **Report Date:** 2026-04-23

        ## Executive Summary
        April 23 summary covering parametric insurance and climate finance.
        """,
    )
    _write(
        wiki_dir / "parametric-insurance.md",
        """
        # Parametric Insurance

        > Index-triggered products keep expanding.
        """,
    )
    _write(
        wiki_dir / "index.md",
        """
        # Wiki Index

        _Last updated: 2026-04-21 - 1 pages + 1 daily report pages_

        ## Daily Reports

        | Date | Report | Status |
        |------|--------|--------|
        | 2026-04-21 | [[climate-monitor-2026-04-21]] | ✅ |

        ## Concepts

        | Page | Summary | Updated |
        |------|---------|---------|
        | [[parametric-insurance]] | Index-triggered products keep expanding. | 2026-04-21 |

        _Last updated: 2026-04-21_
        """,
    )

    result = sync_source_wiki(source_dir=source_dir, wiki_dir=wiki_dir)

    assert result.latest_date == "2026-04-23"
    assert result.topic_pages == 1
    assert result.daily_pages == 3
    assert result.source_days == 2
    assert result.missing_days == ["2026-04-22"]

    april_21 = (wiki_dir / "climate-monitor-2026-04-21.md").read_text(encoding="utf-8")
    assert "# Climate Monitor - 2026-04-21" in april_21
    assert "Source: [[sources/climate-monitor-2026-04-21]]" in april_21
    assert "April 21 summary with linked evidence and strong signal." in april_21

    april_22 = (wiki_dir / "climate-monitor-2026-04-22.md").read_text(encoding="utf-8")
    assert "**Report Date:** 2026-04-22" in april_22
    assert "Source: missing" in april_22
    assert "No report - source file missing for this date." in april_22

    april_23 = (wiki_dir / "climate-monitor-2026-04-23.md").read_text(encoding="utf-8")
    assert "Source: [[sources/climate-monitor-2026-04-23]]" in april_23
    assert "April 23 summary covering parametric insurance and climate finance." in april_23

    index_text = (wiki_dir / "index.md").read_text(encoding="utf-8")
    assert "_Last updated: 2026-04-23 - 1 pages + 3 daily report pages_" in index_text
    assert "| 2026-04-21 | [[climate-monitor-2026-04-21]] | ✅ |" in index_text
    assert "| 2026-04-22 | [[climate-monitor-2026-04-22]] | ⚠️ No report |" in index_text
    assert "| 2026-04-23 | [[climate-monitor-2026-04-23]] | ✅ |" in index_text
    assert "## Concepts" in index_text
    assert "| [[parametric-insurance]] | Index-triggered products keep expanding. | 2026-04-21 |" in index_text
    assert index_text.rstrip().endswith("_Last updated: 2026-04-23_")
