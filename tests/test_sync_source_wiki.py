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
        April 21 summary with [linked evidence](https://example.com) and **strong** signal. <br>
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


def test_weekly_cadence_renders_only_existing_dates_and_weekly_labels(tmp_path):
    """Weekly cadence must not fill the gap between two reports 7 days apart."""
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"

    for day in ("2026-08-03", "2026-08-10"):
        _write(
            source_dir / f"climate-monitor-{day}.md",
            f"""
            # Weekly Climate & Actuarial Monitor
            **Report Date:** {day}

            ## Executive Summary
            Sites checked: 57, succeeded: 57, failed: 0 for {day}.
            """,
        )

    result = sync_source_wiki(source_dir=source_dir, wiki_dir=wiki_dir, cadence="weekly")

    assert result.daily_pages == 2
    assert result.source_days == 2
    assert result.missing_days == []
    # No phantom pages for the six intervening weekdays.
    assert not (wiki_dir / "climate-monitor-2026-08-05.md").exists()

    page = (wiki_dir / "climate-monitor-2026-08-10.md").read_text(encoding="utf-8")
    assert "#climate-monitor #weekly-report #2026-08-10" in page
    assert "Sites checked: 57" in page

    index_text = (wiki_dir / "index.md").read_text(encoding="utf-8")
    assert "## Weekly Reports" in index_text
    assert "weekly report pages_" in index_text
    assert "No report" not in index_text


def test_weekly_cadence_prunes_sourceless_legacy_pages(tmp_path):
    """Placeholder pages left by the old daily grid are removed under weekly."""
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"

    _write(
        source_dir / "climate-monitor-2026-08-10.md",
        """
        # Weekly Climate & Actuarial Monitor
        **Report Date:** 2026-08-10

        ## Executive Summary
        Real weekly report.
        """,
    )
    _write(
        wiki_dir / "climate-monitor-2026-04-11.md",
        """
        # Climate Monitor - 2026-04-11
        Source: missing

        ## Summary
        No report - source file missing for this date.
        """,
    )

    result = sync_source_wiki(source_dir=source_dir, wiki_dir=wiki_dir, cadence="weekly")

    assert result.pruned_pages == ["climate-monitor-2026-04-11.md"]
    assert not (wiki_dir / "climate-monitor-2026-04-11.md").exists()
    assert result.daily_pages == 1


def test_weekly_cadence_can_keep_sourceless_pages(tmp_path):
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"

    _write(
        source_dir / "climate-monitor-2026-08-10.md",
        """
        # Weekly Climate & Actuarial Monitor
        **Report Date:** 2026-08-10

        ## Executive Summary
        Real weekly report.
        """,
    )
    _write(wiki_dir / "climate-monitor-2026-04-11.md", "# Climate Monitor - 2026-04-11\n")

    result = sync_source_wiki(
        source_dir=source_dir,
        wiki_dir=wiki_dir,
        cadence="weekly",
        prune_sourceless=False,
    )

    assert result.pruned_pages == []
    assert (wiki_dir / "climate-monitor-2026-04-11.md").exists()
    assert result.daily_pages == 2


def test_daily_cadence_still_fills_gaps(tmp_path):
    """Regression guard: the historical daily behavior is unchanged."""
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"

    for day in ("2026-04-21", "2026-04-23"):
        _write(
            source_dir / f"climate-monitor-{day}.md",
            f"""
            # Daily Climate Monitor
            **Report Date:** {day}

            ## Executive Summary
            Summary for {day}.
            """,
        )

    result = sync_source_wiki(source_dir=source_dir, wiki_dir=wiki_dir, cadence="daily")

    assert result.daily_pages == 3
    assert result.missing_days == ["2026-04-22"]
    gap = (wiki_dir / "climate-monitor-2026-04-22.md").read_text(encoding="utf-8")
    assert "No report - source file missing for this date." in gap
    assert "#daily-report" in gap
