import html
import os
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import CondPageBreak, HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .errors import GenerationError
from .io import atomic_replace
from .summary import format_scope_line


NAVY = colors.HexColor("#0b3d62")
TEAL = colors.HexColor("#1f6f8b")
LIGHT = colors.HexColor("#f4f7f9")
RULE = colors.HexColor("#d7dee4")
GREY = colors.HexColor("#47535f")
WHITE = colors.white
MARGIN = 18 * mm
FRAME_WIDTH = A4[0] - (2 * MARGIN)


ASCII_REPLACEMENTS = str.maketrans(
    {
        "—": "-",
        "–": "-",
        "−": "-",
        "→": "->",
        "←": "<-",
        "↔": "<->",
        "•": "*",
        "…": "...",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "\u00a0": " ",
    }
)


def ascii_display_text(value: str) -> str:
    translated = str(value).translate(ASCII_REPLACEMENTS)
    normalized = unicodedata.normalize("NFKD", translated)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_value.split())


def _safe(value: str) -> str:
    return html.escape(ascii_display_text(value))


def _page_footer(pdf_canvas, document) -> None:
    pdf_canvas.saveState()
    pdf_canvas.setStrokeColor(RULE)
    pdf_canvas.setLineWidth(0.5)
    pdf_canvas.line(MARGIN, 14 * mm, A4[0] - MARGIN, 14 * mm)
    pdf_canvas.setFillColor(GREY)
    pdf_canvas.setFont("Helvetica", 7.5)
    pdf_canvas.drawString(MARGIN, 10 * mm, "Weekly Climate & Actuarial Monitor - Supranational Organizations")
    pdf_canvas.drawRightString(A4[0] - MARGIN, 10 * mm, f"Page {document.page}")
    pdf_canvas.restoreState()


def _styles():
    base = getSampleStyleSheet()
    return {
        "cover_kicker": ParagraphStyle(
            "ClimateCoverKicker",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#a9d6e5"),
            spaceAfter=4,
        ),
        "cover_title": ParagraphStyle(
            "ClimateCoverTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=17,
            leading=21,
            alignment=TA_LEFT,
            textColor=WHITE,
            spaceAfter=5,
        ),
        "cover_meta": ParagraphStyle(
            "ClimateCoverMeta",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=12,
            textColor=colors.HexColor("#cfe6ee"),
        ),
        "section": ParagraphStyle(
            "ClimateSection",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            textColor=NAVY,
            spaceBefore=4,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "ClimateBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=13,
            textColor=GREY,
            spaceAfter=5,
        ),
        "card_title": ParagraphStyle(
            "ClimateCardTitle",
            parent=base["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            textColor=NAVY,
            spaceAfter=4,
        ),
        "card_body": ParagraphStyle(
            "ClimateCardBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=12,
            textColor=GREY,
            spaceAfter=5,
        ),
        "snapshot_label": ParagraphStyle(
            "ClimateSnapshotLabel",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=11,
            textColor=NAVY,
        ),
        "snapshot_value": ParagraphStyle(
            "ClimateSnapshotValue",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            textColor=GREY,
        ),
    }


def _section_header(title: str, styles) -> list:
    return [
        Paragraph(_safe(title), styles["section"]),
        HRFlowable(width="100%", thickness=1, color=RULE, spaceBefore=0, spaceAfter=7),
    ]


def _highlight_card(
    item: dict[str, str],
    number: int,
    styles,
    semantics: Mapping[str, Any] | None = None,
) -> KeepTogether:
    semantics = semantics or {}
    url = _safe(item["url"])
    content = [
        Paragraph(
            f'<link href="{url}" color="#1a73e8">{number}. {_safe(item["title"])}</link>',
            styles["card_title"],
        ),
    ]
    if item["summary"]:
        content.append(Paragraph(_safe(item["summary"]), styles["card_body"]))
    categories = semantics.get("categories") or []
    keywords = semantics.get("keywords") or []
    if categories:
        content.append(
            Paragraph("Categories: " + _safe(", ".join(categories)), styles["card_body"])
        )
    if keywords:
        content.append(
            Paragraph("Keywords: " + _safe(", ".join(keywords)), styles["card_body"])
        )
    card = Table([[content]], colWidths=[FRAME_WIDTH], hAlign="LEFT")
    card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                ("LINEBEFORE", (0, 0), (0, -1), 3, TEAL),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return KeepTogether([card, Spacer(1, 2 * mm)])


def _monitoring_snapshot(summary: dict[str, Any], styles) -> Table:
    sites = summary["report"].get("sites", {})
    rows = [
        ("Sites checked", sites.get("checked", "-")),
        ("Succeeded", sites.get("succeeded", "-")),
        ("Failed", sites.get("failed", "-")),
        ("Pillar A updates", sum(item["pillar"] == "A" for item in summary["highlights"])),
        ("Pillar B updates", sum(item["pillar"] == "B" for item in summary["highlights"])),
    ]
    table = Table(
        [
            [Paragraph(_safe(label), styles["snapshot_label"]), Paragraph(_safe(str(value)), styles["snapshot_value"])]
            for label, value in rows
        ],
        colWidths=[FRAME_WIDTH * 0.42, FRAME_WIDTH * 0.58],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                ("GRID", (0, 0), (-1, -1), 0.5, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def render_pdf(summary: dict[str, Any], output: Path) -> None:
    output = Path(output)
    temporary: str | None = None
    styles = _styles()
    cover_content = [
        Paragraph("IAA WEEKLY CLIMATE NEWSLETTER", styles["cover_kicker"]),
        Paragraph(_safe(summary["report"]["title"]), styles["cover_title"]),
        Paragraph(
            f"Report week of {_safe(summary['report']['date'])} - {_safe(format_scope_line(summary))}",
            styles["cover_meta"],
        ),
    ]
    cover = Table([[cover_content]], colWidths=[FRAME_WIDTH], hAlign="LEFT")
    cover.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                ("LEFTPADDING", (0, 0), (-1, -1), 16),
                ("RIGHTPADDING", (0, 0), (-1, -1), 16),
                ("TOPPADDING", (0, 0), (-1, -1), 14),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
            ]
        )
    )
    accent = Table([[""]], colWidths=[FRAME_WIDTH], rowHeights=[4], hAlign="LEFT")
    accent.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), TEAL)]))
    story = [cover, accent, Spacer(1, 7 * mm), *_section_header("Executive Summary", styles)]
    for item in summary["executive_summary"]:
        story.append(Paragraph(_safe(item), styles["body"]))
    story.append(Spacer(1, 3 * mm))
    story.extend(_section_header("Monitoring Snapshot", styles))
    story.extend([_monitoring_snapshot(summary, styles), Spacer(1, 3 * mm)])
    for item in summary.get("monitoring_notes", []):
        story.append(Paragraph(f"* {_safe(item)}", styles["body"]))
    if summary.get("monitoring_notes"):
        story.append(Spacer(1, 2 * mm))

    for pillar in ("A", "B"):
        highlights = [item for item in summary["highlights"] if item["pillar"] == pillar]
        if not highlights:
            continue
        semantics_index = summary.get("article_semantics", {})
        cards = [
            _highlight_card(item, number, styles, semantics_index.get(item["url"]))
            for number, item in enumerate(highlights, start=1)
        ]
        story.append(CondPageBreak(50 * mm))
        story.extend(_section_header(f"Pillar {pillar}", styles))
        story.extend(cards)

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        os.close(descriptor)
        document = SimpleDocTemplate(
            temporary,
            pagesize=A4,
            leftMargin=MARGIN,
            rightMargin=MARGIN,
            topMargin=MARGIN,
            bottomMargin=MARGIN,
            title=ascii_display_text(summary["report"]["title"]),
            author="IAA Weekly Climate Newsletter",
        )

        def deterministic_canvas(*args, **kwargs):
            kwargs["invariant"] = 1
            return canvas.Canvas(*args, **kwargs)

        document.build(
            story,
            onFirstPage=_page_footer,
            onLaterPages=_page_footer,
            canvasmaker=deterministic_canvas,
        )
        atomic_replace(Path(temporary), output)
    except Exception as exc:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        raise GenerationError("PDF generation failed") from exc
