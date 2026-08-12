import html
import os
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

from .errors import GenerationError
from .io import atomic_replace


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
    pdf_canvas.setFont("Helvetica", 8)
    pdf_canvas.drawCentredString(LETTER[0] / 2, 0.4 * inch, f"Page {document.page}")
    pdf_canvas.restoreState()


def render_pdf(summary: dict[str, Any], output: Path) -> None:
    output = Path(output)
    temporary: str | None = None
    styles = getSampleStyleSheet()
    url_style = styles["BodyText"].clone("ClimateDeliveryURL")
    url_style.wordWrap = "CJK"
    story = [
        Paragraph(_safe(summary["report"]["title"]), styles["Title"]),
        Paragraph(f"Report date: {_safe(summary['report']['date'])}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
        Paragraph("Executive Summary", styles["Heading2"]),
    ]
    for item in summary["executive_summary"]:
        story.append(Paragraph(f"* {_safe(item)}", styles["BodyText"]))
    story.extend([Spacer(1, 0.15 * inch), Paragraph("Highlights", styles["Heading2"])])
    for item in summary["highlights"]:
        highlight = [Paragraph(f"Pillar {_safe(item['pillar'])}: {_safe(item['title'])}", styles["Heading3"])]
        if item["summary"]:
            highlight.append(Paragraph(_safe(item["summary"]), styles["BodyText"]))
        highlight.extend([Paragraph(_safe(item["url"]), url_style), Spacer(1, 0.08 * inch)])
        story.append(KeepTogether(highlight))
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        os.close(descriptor)
        document = SimpleDocTemplate(temporary, pagesize=LETTER, title=ascii_display_text(summary["report"]["title"]))

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
