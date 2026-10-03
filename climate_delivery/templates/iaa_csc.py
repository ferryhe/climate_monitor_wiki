"""IAA CSC v1: local fonts, immutable values, ReportLab only."""
import html
import io
import re
from itertools import groupby
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Frame, PageBreak, PageTemplate, NextPageTemplate, Paragraph,
                              Spacer, Table, TableStyle, IndexingFlowable)
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.pdfgen.canvas import Canvas

from climate_delivery.errors import GenerationError
from climate_delivery.io import atomic_write_bytes
from . import render_identity, rendering_metadata
from .model import Report

NAVY = colors.HexColor("#073563")
GOLD = colors.HexColor("#eba900")
LIGHT = colors.HexColor("#f2f5f8")
INK = colors.HexColor("#263544")
MARGIN = 18 * mm
WIDTH = A4[0] - 2 * MARGIN
ASSETS = Path(__file__).parent / "assets" / "v1"
DISCLAIMER = ("This report presents frozen source material and may include AI-assisted summaries. "
              "Consult the cited originals and independently evaluate the information before relying on it. "
              "Inclusion does not imply endorsement by the IAA or CSC.")
PURPOSE = ("Prepared for climate and sustainability monitoring and actuarial discussion. "
           "The report preserves the supplied summaries, dates and citations; missing source data is identified.")


class _UnicodeFont(TTFont):
    def addObjects(self, document):
        super().addObjects(document)
        # ReportLab 5 emits non-BMP ToUnicode values as <1F321>, rather than
        # UTF-16BE surrogate pairs. Repair only those generated font mappings;
        # the glyph/content streams are unchanged, and extraction keeps emoji.
        for name, stream in document.idToObject.items():
            if name.startswith("toUnicodeCMap:"):
                stream.content = re.sub(r"<([0-9A-F]{5,6})>",
                    lambda match: "<" + chr(int(match[1], 16)).encode("utf-16-be").hex().upper() + ">",
                    stream.content)


def _fonts() -> None:
    for name, filename in (("IAARegular", "DejaVuSans.ttf"), ("IAABold", "DejaVuSans-Bold.ttf"),
                           ("IAACJK", "NotoSansSC-Regular.ttf"), ("IAASymbols", "NotoSansSymbols2-Regular.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            try:
                pdfmetrics.registerFont(_UnicodeFont(name, ASSETS / filename))
            except Exception as exc:
                raise GenerationError(f"template font could not be loaded: {filename}") from exc


def _markup(value: str, *, bold: bool = False) -> str:
    """Embed only fonts with actual glyphs; fail rather than deleting text."""
    primary = "IAABold" if bold else "IAARegular"
    fragments = []
    for character in str(value):
        if character == "\n":
            fragments.append((primary, "<br/>"))
            continue
        if character == "\t":
            character = " "
        font = next((name for name in (primary, "IAACJK", "IAASymbols")
                     if ord(character) in pdfmetrics.getFont(name).face.charToGlyph), None)
        if font is None:
            raise GenerationError(f"template font has no glyph for U+{ord(character):04X}")
        fragments.append((font, html.escape(character, quote=True)))
    return "".join(f'<font name="{font}">' + "".join(part for _, part in items) + "</font>"
                   for font, items in groupby(fragments, key=lambda part: part[0]))


class _PageTotal(IndexingFlowable):
    def __init__(self, document):
        super().__init__()
        self.document = document
        self.total = 0
        self.previous = -1

    def beforeBuild(self):
        self.previous = self.total

    def afterBuild(self):
        self.total = self.document.page

    def isSatisfied(self):
        return self.previous == self.total

    def draw(self):
        pass


class _Document(BaseDocTemplate):
    def afterFlowable(self, flowable):
        if not isinstance(flowable, Paragraph) or flowable.style.name not in {"H1", "H2", "H3"}:
            return
        level = {"H1": 0, "H2": 1, "H3": 2}[flowable.style.name]
        key = f"heading-{self.seq.nextf('heading')}"
        self.canv.bookmarkPage(key)
        text = flowable.getPlainText()
        self.canv.addOutlineEntry(text, key, level=level, closed=False)
        # TOC is Paragraph markup; escape source text, including <, &, Chinese.
        self.notify("TOCEntry", (level, _markup(text), self.page, key))


def render_report(report: Report, output: str | Path) -> None:
    _fonts()
    metadata = rendering_metadata()
    styles = {
        "body": ParagraphStyle("Body", fontName="IAARegular", fontSize=9, leading=13,
                               textColor=INK, spaceAfter=6, splitLongWords=True),
        "small": ParagraphStyle("Small", fontName="IAARegular", fontSize=7.5, leading=10.5,
                                textColor=INK, spaceAfter=4, splitLongWords=True),
        "H1": ParagraphStyle("H1", fontName="IAABold", fontSize=14, leading=20,
                             textColor=colors.white, backColor=NAVY, borderPadding=(6, 8, 6, 8),
                             borderColor=GOLD, borderWidth=.5,
                             spaceBefore=13, spaceAfter=12, keepWithNext=True),
        "H2": ParagraphStyle("H2", fontName="IAABold", fontSize=12, leading=17,
                             textColor=NAVY, spaceBefore=12, spaceAfter=6, keepWithNext=True),
        "H3": ParagraphStyle("H3", fontName="IAABold", fontSize=10, leading=14,
                             textColor=NAVY, spaceBefore=7, spaceAfter=4, keepWithNext=True),
        "title": ParagraphStyle("CoverTitle", fontName="IAABold", fontSize=24, leading=30,
                                textColor=colors.white, spaceAfter=16, alignment=TA_CENTER),
        "cover": ParagraphStyle("Cover", fontName="IAARegular", fontSize=10, leading=15,
                                textColor=colors.white, spaceAfter=8),
        "gold": ParagraphStyle("Gold", fontName="IAABold", fontSize=10, leading=15,
                               textColor=GOLD, spaceAfter=10, alignment=TA_CENTER),
    }
    styles["contents"] = ParagraphStyle("ContentsTitle", parent=styles["H1"])

    def p(value, style="body", bold=False):
        return Paragraph(_markup(str(value), bold=bold), styles[style])

    def heading(value, level=1):
        return p(value, f"H{level}", bold=True)

    def table(headers, rows, widths):
        header_style = ParagraphStyle("TableHeader", parent=styles["small"], textColor=colors.white,
                                      fontName="IAABold", keepWithNext=False)
        cells = [[Paragraph(_markup(label, bold=True), header_style) for label in headers]]
        cells.extend([[p(value, "small") for value in row] for row in rows])
        result = Table(cells, colWidths=[WIDTH * width for width in widths], repeatRows=1,
                       splitByRow=1, splitInRow=1, hAlign="LEFT")
        result.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [LIGHT, colors.white]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 2, GOLD),
            ("LINEBELOW", (0, 1), (-1, -1), .35, colors.HexColor("#cdd5df")),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        return result

    buffer = io.BytesIO()
    document = _Document(buffer, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=MARGIN, bottomMargin=23 * mm, title=report.title,
                         author="IAA Weekly Climate Newsletter")
    total = _PageTotal(document)

    def on_page(pdf, doc):
        pdf.saveState()
        if doc.pageTemplate.id == "cover":
            pdf.setFillColor(NAVY)
            pdf.rect(12 * mm, 20 * mm, A4[0] - 24 * mm, A4[1] - 32 * mm, fill=1, stroke=0)
            pdf.setFillColor(GOLD)
            pdf.rect(12 * mm, A4[1] - 18 * mm, A4[0] - 24 * mm, 6 * mm, fill=1, stroke=0)
        pdf.setStrokeColor(GOLD)
        pdf.line(MARGIN, 16 * mm, A4[0] - MARGIN, 16 * mm)
        footer = p(f"IAA CSC Climate Intelligence Report · {report.edition}", "small")
        _, height = footer.wrap(WIDTH - 95, 35)
        footer.drawOn(pdf, MARGIN, 12 * mm - height)
        pdf.setFont("IAARegular", 7.5)
        pdf.setFillColor(INK)
        pdf.drawRightString(A4[0] - MARGIN, 10 * mm, f"Page {doc.page} of {total.total}")
        pdf.restoreState()

    frame = Frame(document.leftMargin, document.bottomMargin, document.width, document.height, id="report")
    document.addPageTemplates([PageTemplate(id="cover", frames=frame, onPage=on_page),
                               PageTemplate(id="report", frames=frame, onPage=on_page)])
    story = [total, Spacer(1, 15 * mm), p("IAA  |  CSC", "gold", True),
             p("CLIMATE & SUSTAINABILITY COMMITTEE", "gold"), Spacer(1, 9 * mm),
             p(report.title, "title", True), p(report.edition, "gold", True), Spacer(1, 8 * mm),
             p("Report window: " + report.window, "cover"),
             p("Date of run: " + (report.run_date or "not provided in frozen input"), "cover")]
    story.extend(p(f"{label}: {value}", "cover") for label, value in report.statistics)
    story.extend([Spacer(1, 6 * mm), p("ARTIFICIAL INTELLIGENCE DISCLAIMER", "gold", True),
                  p(DISCLAIMER, "cover"), p("PURPOSE", "gold", True), p(PURPOSE, "cover"),
                  NextPageTemplate("report"), PageBreak(), p("Contents", "contents", True)])
    toc = TableOfContents()
    toc.levelStyles = [ParagraphStyle(f"TOC{level}", parent=styles["body"], leftIndent=12 * level,
                                     firstLineIndent=0, rightIndent=25, spaceBefore=3, spaceAfter=4,
                                     fontSize=9 if level == 0 else 8, leading=13 if level == 0 else 11)
                       for level in range(3)]
    story.extend([toc, PageBreak(), heading("Executive Summary")])
    story.extend(p(line) for line in report.executive_summary)
    if not report.executive_summary:
        story.append(p("Executive summary not provided in frozen input."))
    # Summary citations remain explicit even when the stored prose has no inline locators.
    def citations(values):
        result = []
        for citation in values:
            text = _markup(citation.label)
            if citation.url:
                label = text if citation.label == citation.url else text + " — " + _markup(citation.url)
                text = f'<link href="{html.escape(citation.url, quote=True)}" color="#124b7e">{label}</link>'
            result.append(Paragraph(text, styles["small"]))
        return result
    if report.summary_citations:
        story.append(p("Executive summary source references", "small", True))
        story.extend(citations(tuple(dict.fromkeys(report.summary_citations))))
    story.extend([p("Input identity: " + report.input_id, "small"),
                  p("Input SHA-256: " + report.input_sha256, "small"),
                  p("Template: " + metadata["template_id"] + " / " + metadata["template_version"], "small"),
                  p("Renderer: " + render_identity(), "small")])
    if report.statistics or report.coverage_notes:
        story.append(heading("Monitoring Snapshot", 2))
        if report.statistics:
            story.append(table(("Measure", "Frozen value"), report.statistics, (.55, .45)))
        story.extend(p(line) for line in report.coverage_notes)
    story.extend([PageBreak(), heading("Key Dates")])
    story.append(p("Date markers use the frozen run date: " + (report.run_date or "unavailable") + ".", "small"))
    story.extend(p(line) for line in report.date_notes)
    if report.key_dates:
        story.append(table(("Date(s)", "Event / deadline", "Institution", "Relevance", "Source"), report.key_dates,
                           (.14, .23, .14, .23, .26)))
    story.extend([PageBreak(), heading("Updates by Publisher / Institution")])
    if not report.updates:
        story.append(p("No selected updates in the frozen input."))
    sorted_updates = sorted(enumerate(report.updates, 1), key=lambda pair: (pair[1].institution.casefold(), pair[1].topic.casefold(), pair[0]))
    number = 0
    for institution, group in groupby(sorted_updates, key=lambda pair: pair[1].institution):
        story.append(heading(institution, 2))
        for topic, topic_group in groupby(group, key=lambda pair: pair[1].topic):
            if topic != institution:
                story.append(heading(topic, 3))
            for index, update in topic_group:
                number += 1
                # A plain Paragraph can split across pages; do not cage long updates in a card.
                story.append(p(f"{number}. {update.title}", bold=True))
                story.append(p("Publication date: " + (update.publication_date or "Article publication date unconfirmed"), "small"))
                if update.coverage_period:
                    story.append(p("PDF coverage period: " + " through ".join(update.coverage_period), "small"))
                if update.article_id:
                    story.append(p("Article ID: " + update.article_id, "small"))
                story.append(p("Content version: " + (update.content_version or "not provided"), "small"))
                if update.content_sha256:
                    story.append(p("Content SHA-256: " + update.content_sha256, "small"))
                story.extend(p(f"{key}: {value}", "small") for key, value in update.metadata)
                story.extend(p(part) for part in update.paragraphs)
                if not update.paragraphs:
                    story.append(p("Stored summary/body not provided."))
                story.append(p("Sources", "small", True))
                story.extend(citations(update.citations))
                story.append(Spacer(1, 4 * mm))
    if report.cross_cutting_watch:
        story.append(heading("Cross-Cutting Watch"))
        story.extend(p(line) for line in report.cross_cutting_watch)
    for title, headers, rows, widths in (
        ("Appendix A — Source Coverage", ("Institution", "Status", "Evidence / limitation"), report.coverage, (.25, .2, .55)),
        ("Appendix B — Access / Route Corrections", ("Source", "Correction"), report.route_corrections, (.3, .7)),
        ("Appendix C — Glossary", ("Term", "Definition"), report.glossary, (.3, .7)),
    ):
        if rows:
            story.extend([PageBreak(), heading(title), table(headers, rows, widths)])
    try:
        def deterministic_canvas(*args, **kwargs):
            kwargs["invariant"] = 1
            return Canvas(*args, **kwargs)
        document.multiBuild(story, canvasmaker=deterministic_canvas)
        atomic_write_bytes(Path(output), buffer.getvalue())
    except Exception as exc:
        raise GenerationError("IAA CSC PDF generation failed") from exc
