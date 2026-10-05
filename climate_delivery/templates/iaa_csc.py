"""Approved reference layout. No database, network or summary generation."""
import html
import io
import re
from datetime import date, timedelta
from itertools import groupby
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, IndexingFlowable,
    NextPageTemplate, PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle)

from climate_delivery.errors import GenerationError
from climate_delivery.io import atomic_write_bytes
from .model import Report
from .adapters import calendar_date_bounds

W, H = 595.92, 842.88
LEFT, TOP, WIDTH = 45.75, 51.75, 504.72
NAVY, GOLD = colors.HexColor("#003366"), colors.HexColor("#f5a800")
PALE, GREY, LINE = [colors.HexColor(x) for x in ("#f4f7fa", "#586579", "#cbd5e1")]
ASSETS = Path(__file__).parent / "assets"
DISCLAIMER = ("This report presents frozen source material and may include AI-assisted summaries. "
    "Publication dates and coverage limitations are shown as recorded. Consult the cited originals "
    "and independently evaluate the information before relying on it. Inclusion does not imply "
    "endorsement or review by the IAA or CSC.")
PURPOSE = ("Prepared for climate and sustainability monitoring and actuarial discussion. "
    "Source summaries, caveats, dates and citations are retained. Missing source data is identified; "
    "a report window does not establish that every date or institution was monitored.")


class _UnicodeFont(TTFont):
    def addObjects(self, document):
        super().addObjects(document)
        # ReportLab 5 needs UTF-16BE surrogate pairs for non-BMP extraction.
        for name, stream in document.idToObject.items():
            if name.startswith("toUnicodeCMap:"):
                stream.content = re.sub(r"<([0-9A-F]{5,6})>",
                    lambda m: "<" + chr(int(m[1], 16)).encode("utf-16-be").hex().upper() + ">", stream.content)


def _fonts():
    files = [("IAARegular", "v3/LiberationSans-Regular.ttf"), ("IAABold", "v3/LiberationSans-Bold.ttf"),
        ("IAAItalic", "v3/LiberationSans-Italic.ttf"), ("IAABoldItalic", "v3/LiberationSans-BoldItalic.ttf"),
        ("IAAFallback", "v1/DejaVuSans.ttf"), ("IAACJK", "v1/NotoSansSC-Regular.ttf"),
        ("IAASymbols", "v1/NotoSansSymbols2-Regular.ttf")]
    for name, filename in files:
        if name not in pdfmetrics.getRegisteredFontNames():
            try:
                pdfmetrics.registerFont(_UnicodeFont(name, ASSETS / filename))
            except Exception as exc:
                raise GenerationError(f"template font could not be loaded: {filename}") from exc
    pdfmetrics.registerFontFamily("IAARegular", normal="IAARegular", bold="IAABold",
        italic="IAAItalic", boldItalic="IAABoldItalic")


def _markup(value, *, bold=False):
    primary = "IAABold" if bold else "IAARegular"
    fragments = []
    for character in str(value):
        if character == "\n":
            fragments.append((primary, "<br/>"))
            continue
        if character == "\t":
            character = " "
        if character in {"📎", "🍪"}:
            fragments.append((primary, "[attachment]" if character == "📎" else "[cookie]"))
            continue
        font = next((name for name in (primary, "IAAFallback", "IAACJK", "IAASymbols")
            if ord(character) in pdfmetrics.getFont(name).face.charToGlyph), None)
        if font is None:
            raise GenerationError(f"template font has no glyph for U+{ord(character):04X}")
        fragments.append((font, html.escape(character, quote=True)))
    return "".join(f'<font name="{font}">' + "".join(part for _, part in items) + "</font>"
        for font, items in groupby(fragments, key=lambda part: part[0]))


def _prose(value):
    return re.sub(r"\s+", " ", value).strip()


class _PageTotal(IndexingFlowable):
    def __init__(self, document):
        super().__init__()
        self.document, self.total, self.previous = document, 0, -1
    def beforeBuild(self):
        self.previous = self.total
    def afterBuild(self):
        self.total = self.document.page
    def isSatisfied(self):
        return self.previous == self.total
    def draw(self):
        pass


class _Banner(Flowable):
    def __init__(self, title, key, style):
        super().__init__()
        self.title, self.key = title, key
        self.paragraph = Paragraph(_markup(title, bold=True), style)
        self.keepWithNext, self.spaceAfter = True, 12
    def wrap(self, width, height):
        self.width = width
        _, text_height = self.paragraph.wrap(width - 44, height)
        self.height = max(50.25, text_height + 24)
        return width, self.height
    def draw(self):
        self.canv.setFillColor(NAVY)
        self.canv.rect(0, 0, self.width, self.height, fill=1, stroke=0)
        self.canv.setFillColor(GOLD)
        self.canv.rect(0, 0, 7.5, self.height, fill=1, stroke=0)
        self.paragraph.drawOn(self.canv, 22, (self.height - self.paragraph.height) / 2)


class _Circle(Flowable):
    def __init__(self, number):
        super().__init__()
        self.number, self.width, self.height = number, 25, 25
    def draw(self):
        self.canv.setFillColor(GOLD)
        self.canv.circle(11.5, 12.5, 11.5, fill=1, stroke=0)
        self.canv.setFillColor(NAVY)
        self.canv.setFont("IAABold", 8.5)
        self.canv.drawCentredString(11.5, 9.3, str(self.number))


class _Document(BaseDocTemplate):
    def afterFlowable(self, flowable):
        if key := getattr(flowable, "key", None):
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flowable.title, key, level=getattr(flowable, "outline_level", 0), closed=False)


def render_report(report: Report, output: str | Path) -> None:
    _fonts()
    styles = {}
    for name, font, size, leading, color in [
        ("body", "IAARegular", 9.5, 14.25, colors.HexColor("#202020")),
        ("small", "IAARegular", 8.3, 12.45, GREY), ("italic", "IAAItalic", 8.5, 12.5, GREY),
        ("title", "IAABold", 10, 14, NAVY), ("org", "IAABold", 14.5, 20, NAVY),
        ("label", "IAABold", 9.5, 14, NAVY), ("cell", "IAARegular", 8.3, 12.45, colors.HexColor("#202020")),
        ("thead", "IAABold", 7.5, 11, colors.white), ("toc", "IAARegular", 9, 13.5, NAVY),
        ("banner", "IAABold", 15, 20, colors.white)]:
        styles[name] = ParagraphStyle(name, fontName=font, fontSize=size, leading=leading,
            textColor=color, spaceAfter=0 if name in {"cell", "thead"} else 7)
    styles["body"].alignment = TA_JUSTIFY
    for name in ("org", "label"):
        styles[name].keepWithNext = True
        styles[name].spaceBefore = 12
    for name in ("body", "small", "italic"):
        styles[name + "update"] = ParagraphStyle(name + "update", parent=styles[name], leftIndent=31.5)
    styles["badge"] = ParagraphStyle("badge", parent=styles["smallupdate"], fontSize=7.3, leading=11,
        backColor=PALE, borderPadding=4, spaceAfter=8, textColor=NAVY, keepWithNext=True)
    styles["caveat"] = ParagraphStyle("caveat", parent=styles["smallupdate"], backColor=colors.HexColor("#fff4ea"),
        borderPadding=8, spaceBefore=5, spaceAfter=10)

    def p(value, style="body", bold=False, raw=False):
        return Paragraph(value if raw else _markup(value, bold=bold), styles[style])
    def link(label, url, bold=False):
        return f'<link href="{html.escape(url, quote=True)}" color="#003366"><u>{_markup(label, bold=bold)}</u></link>'
    def banner(title, key):
        return _Banner(title, key, styles["banner"])
    def table(headers, rows, widths, highlights=()):
        result = Table([[p(x, "thead", True) for x in headers], *rows], colWidths=widths,
            repeatRows=1, splitByRow=1, splitInRow=1, hAlign="LEFT")
        commands = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]), ("LINEBELOW", (0, 1), (-1, -1), .45, LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]
        commands.extend(("LINEBEFORE", (0, i + 1), (0, i + 1), 2, GOLD) for i in highlights)
        result.setStyle(TableStyle(commands))
        return result
    def citations(values, style="small", imported=False):
        result = []
        urls = tuple(dict.fromkeys(citation.url for citation in values if citation.url))
        for index, url in enumerate(urls):
            text = link(url, url)
            if imported and index == len(urls) - 1:
                text += " | PDF import"
            result.append(p("Source: " + text, style, raw=True))
        if not urls:
            result.append(p("Source URL not provided" + (" | PDF import" if imported else ""), style))
        return result

    groups = [(name, tuple(items)) for name, items in groupby(
        sorted(report.updates, key=lambda u: (u.institution.casefold(), u.topic.casefold())), key=lambda u: u.institution)]
    sections = [("Executive Summary", "executive"), ("Key Dates", "calendar"), ("Updates by Publisher / Institution", "updates")]
    sections += [(name, f"org{i}") for i, (name, _) in enumerate(groups)]
    if report.cross_cutting_watch:
        sections.append(("Cross-Cutting Watch", "watch"))
    if report.coverage:
        sections.append(("Appendix A - Source Coverage", "coverage"))
    sections.append(("Appendix B - Source Notes", "notes"))
    if report.glossary:
        sections.append(("Appendix C - Glossary", "glossary"))
    buffer = io.BytesIO()
    document = _Document(buffer, pagesize=(W, H), leftMargin=LEFT, rightMargin=LEFT,
        topMargin=TOP, bottomMargin=57, title=report.title, author="IAA Weekly Climate Newsletter", initialFontName="IAARegular")
    total = _PageTotal(document)

    def on_page(c, doc):
        c.saveState()
        c.setFont("IAARegular", 7)
        c.setFillColor(NAVY)
        c.drawString(LEFT, 17, "IAA CSC Climate Risk Intelligence Report")
        c.setFont("IAAItalic", 7)
        c.setFillColor(GREY)
        c.drawCentredString(372, 17, "AI-assisted - see cover")
        c.setFont("IAARegular", 7)
        c.drawRightString(W - LEFT, 17, f"Page {doc.page} of {total.total}")
        if doc.pageTemplate.id == "toc":
            c.setFillColor(PALE)
            c.rect(LEFT, 110, WIDTH, H - TOP - 110, fill=1, stroke=0)
            c.setFillColor(GOLD)
            c.rect(LEFT, 110, 7.5, H - TOP - 110, fill=1, stroke=0)
        if doc.page != 1:
            c.restoreState()
            return
        c.setFillColor(NAVY)
        c.rect(39.75, 39.67, 515.98, 762.71, fill=1, stroke=0)
        c.setFillColor(GOLD)
        c.rect(39.75, 785.88, 515.98, 16.5, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.rect(189.74, H - 175.5, 215.24, 73.5, fill=1, stroke=0)
        c.drawImage(str(ASSETS / "v3/IAA-logo.png"), 205, H - 164.5, 185, 51, mask="auto", preserveAspectRatio=True, anchor="c")
        c.setFont("IAARegular", 8.5)
        c.setFillColor(GOLD)
        c.saveState()
        text = c.beginText()
        text.setTextOrigin(173, H - 225)
        text.setCharSpace(2)
        text.textOut("CLIMATE & SUSTAINABILITY COMMITTEE")
        c.drawText(text)
        c.restoreState()
        c.setFillColor(colors.white)
        c.setFont("IAABold", 27)
        c.drawCentredString(W / 2, H - 273, "Climate Risk")
        c.drawCentredString(W / 2, H - 304, "Intelligence Report")
        edition = Paragraph(_markup(report.edition, bold=True), ParagraphStyle("edition", fontName="IAABold",
            fontSize=13, leading=16, textColor=GOLD, alignment=1))
        _, eh = edition.wrap(424.5, 45)
        edition.drawOn(c, 85.5, H - 338 - eh)
        stats = [("REPORTING PERIOD", report.window), ("DATE OF RUN", report.run_date or "Not provided"),
            ("PUBLISHER GROUPS IN INPUT", str(len(groups))), ("NUMBERED SOURCE UPDATES", str(len(report.updates))),
            ("CALENDAR ENTRIES", str(len(report.key_dates))), ("PUBLICATION DAY UNCONFIRMED", str(sum(u.publication_date is None for u in report.updates)))]
        for i, (label, value) in enumerate(stats):
            y = H - 402 - i * 27
            c.setFont("IAARegular", 7.5)
            c.setFillColor(colors.HexColor("#92abc4"))
            c.drawString(100, y, label)
            para = Paragraph(_markup(value, bold=True), ParagraphStyle("covervalue", fontName="IAABold",
                fontSize=8.3, leading=10, textColor=colors.white, alignment=2))
            _, height = para.wrap(230, 22)
            para.drawOn(c, 280, y + 7 - height)
            c.setStrokeColor(colors.HexColor("#43617f"))
            c.setLineWidth(.4)
            c.line(100, y - 10, 510, y - 10)
        for top, box_height, title, body in [(574, 96, "ARTIFICIAL INTELLIGENCE DISCLAIMER", DISCLAIMER), (681, 61, "PURPOSE", PURPOSE)]:
            c.setStrokeColor(colors.HexColor("#6f754d"))
            c.setLineWidth(.6)
            c.rect(85.5, H - top - box_height, 424.5, box_height, fill=0, stroke=1)
            c.setStrokeColor(GOLD)
            c.setLineWidth(2)
            c.line(85.5, H - top - box_height, 85.5, H - top)
            c.setFont("IAABold", 8)
            c.setFillColor(GOLD)
            c.drawString(101, H - top - 20, title)
            para = Paragraph(_markup(body), ParagraphStyle("coverbody", fontName="IAARegular", fontSize=7.4, leading=11.2, textColor=colors.white))
            _, height = para.wrap(392, box_height - 28)
            para.drawOn(c, 101, H - top - 29 - height)
        c.restoreState()

    body_frame = Frame(LEFT, 57, WIDTH, H - TOP - 57, id="body", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    toc_frame = Frame(72.75, 125, 450.72, H - 68.25 - 125, id="tocbody", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    document.addPageTemplates([PageTemplate(id="main", frames=body_frame, onPage=on_page), PageTemplate(id="toc", frames=toc_frame, onPage=on_page)])
    story = [total, Spacer(1, 1), NextPageTemplate("toc"), PageBreak(), banner("Table of Contents", "toc"), Spacer(1, 8)]
    def toc_lines(items):
        return [p(f'<link href="#{key}">{_markup(title)}</link>', "toc", raw=True) for title, key in items]
    story += toc_lines(sections[:3]) + [p("ORGANISATION SECTIONS (A-Z)", "label", True)]
    org_sections = sections[3:3 + len(groups)]
    midpoint = (len(groups) + 1) // 2
    if org_sections:
        columns = Table([[toc_lines(org_sections[:midpoint]), toc_lines(org_sections[midpoint:])]], colWidths=[225.36, 225.36], splitInRow=1, hAlign="LEFT")
        columns.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 17)]))
        story.append(columns)
    story += [p("APPENDICES / WATCH", "label", True)] + toc_lines(sections[3 + len(groups):])
    story += [NextPageTemplate("main"), PageBreak(), banner("Executive Summary", "executive")]
    story += [p(_prose(line)) for line in report.executive_summary] or [p("Executive summary not provided in frozen input.")]
    tiles = []
    for count, label in [(len(report.updates), "Numbered source updates"),
        (sum(u.publication_date is not None for u in report.updates), "Publication day recorded"),
        (sum(u.publication_date is None for u in report.updates), "Publication day unconfirmed"),
        (len(report.key_dates), "Calendar entries")]:
        tiles.append([Paragraph(str(count), ParagraphStyle("count", fontName="IAABold", fontSize=21,
            leading=27, textColor=GOLD)), Spacer(1, 4), Paragraph(label, ParagraphStyle("tile",
            fontName="IAARegular", fontSize=7.5, leading=11, textColor=colors.white))])
    tile_table = Table([tiles], colWidths=[WIDTH / 4] * 4)
    tile_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12), ("LINEAFTER", (0, 0), (-2, 0), 9, colors.white),
        ("LINEBELOW", (0, 0), (-1, -1), 4, GOLD)]))
    story += [Spacer(1, 12), tile_table, Spacer(1, 16)]
    if report.statistics:
        story += [p("MONITORING SNAPSHOT", "label", True), table(["MEASURE", "FROZEN VALUE"], [[p(a, "cell"), p(b, "cell")] for a, b in report.statistics], [WIDTH * .6, WIDTH * .4])]
    story += [p(_prose(line)) for line in report.coverage_notes] + citations(report.summary_citations)
    story += [PageBreak(), banner("Key Dates", "calendar"), p("Calendar as at " + (report.run_date or "date unavailable") +
        ". Dates within the next 14 days receive a gold rule. Broader windows retain their recorded precision.", "italic")]
    reader_date_notes = [line for line in report.date_notes
        if not any(token in line for token in (" ID:", "SHA-256:", "timezone:", "base date:"))]
    story += [p(line, "small") for line in reader_date_notes]
    for precise in (True, False):
        selected = [(i, row) for i, row in enumerate(report.key_dates, 1)
            if (calendar_date_bounds(row[0])[2] == "day") == precise]
        selected.sort(key=lambda pair: calendar_date_bounds(pair[1][0])[0] or "9999")
        if not selected:
            continue
        if not precise:
            story.append(p("BROADER WINDOWS - DAY UNCONFIRMED", "label", True))
        rows, highlights = [], []
        for index, (number, (when, event, host, relevance, sources)) in enumerate(selected):
            urls = re.findall(r"https?://[^\s]+", sources)
            title = link(event, urls[0]) if urls else _markup(event)
            if "PDF import" in sources:
                title += "<br/>PDF import"
            rows.append([p(when, "cell", True), p(title, "cell", raw=True), p(host, "cell"), p(relevance, "cell")])
            lower, upper, _ = calendar_date_bounds(when)
            if precise and lower and upper and report.run_date and "End date unconfirmed" not in when:
                base = date.fromisoformat(report.run_date)
                if date.fromisoformat(upper) >= base and date.fromisoformat(lower) <= base + timedelta(days=13):
                    highlights.append(index)
        story.append(table(["DATE(S)", "EVENT", "HOST", "RELEVANCE"], rows, [93, 147.74, 67.5, 196.48], highlights))
    story += [PageBreak(), banner("Updates by Publisher / Institution", "updates")]
    if not report.updates:
        story.append(p("No selected updates in the frozen input."))
    numbered = []
    for index, (institution, updates) in enumerate(groups):
        heading = p(institution, "org", True)
        heading.key, heading.title, heading.outline_level = f"org{index}", institution, 1
        story += [heading, p(f"{len(updates)} source update(s) retained in the frozen input.", "italic")]
        previous_topic = None
        for update in updates:
            number = len(numbered) + 1
            numbered.append(update)
            url = next((c.url for c in update.citations if c.url), None)
            title = link(update.title, url, bold=True) if url else _markup(update.title, bold=True)
            row = Table([[_Circle(number), p(title, "title", raw=True)]], colWidths=[31.5, WIDTH - 31.5], splitInRow=1, hAlign="LEFT")
            row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
            row.keepWithNext = True
            if update.topic != previous_topic:
                row.key, row.title, row.outline_level = f"topic-{number}", update.topic, 2
                previous_topic = update.topic
            if update.date_basis == "collection_time":
                labels = ["Collected at: " + (update.collected_at or "Not recorded")]
                if update.publication_date:
                    labels.append("Publication date: " + update.publication_date)
                if update.information_date:
                    labels.append("Information date: " + update.information_date)
            elif update.date_basis == "information_date":
                labels = ["Information date: " + (update.information_date or update.publication_date or "Not recorded")]
                if update.publication_date and update.publication_date != update.information_date:
                    labels.append("Publication date: " + update.publication_date)
            elif update.date_basis == "publication_date":
                labels = ["Publication date: " + (update.publication_date or "Not recorded")]
            else:
                labels = [update.publication_date or "Article publication date unconfirmed"]
            labels.append(update.topic)
            if update.coverage_period and not update.imported_from_pdf:
                labels.append("PDF coverage: " + " through ".join(update.coverage_period))
            story += [row, p(" | ".join(labels), "badge")]
            story += [p(_prose(part), "caveat" if _prose(part).lower().startswith("caveat") else "bodyupdate") for part in update.paragraphs]
            if not update.paragraphs:
                story.append(p("Stored summary/body not provided.", "bodyupdate"))
            story += citations(update.citations, style="smallupdate", imported=update.imported_from_pdf) + [Spacer(1, 14)]
    if report.cross_cutting_watch:
        story += [PageBreak(), banner("Cross-Cutting Watch", "watch")] + [p(_prose(line)) for line in report.cross_cutting_watch]
    if report.coverage:
        story += [PageBreak(), banner("Appendix A - Source Coverage", "coverage"), table(["INSTITUTION", "STATUS", "EVIDENCE / LIMITATION"], [[p(v, "cell") for v in row] for row in report.coverage], [126, 101, WIDTH - 227])]
    story += [PageBreak(), banner("Appendix B - Source Notes", "notes"), p(report.title, "label", True)]
    story += [p(line, "small") for line in [*report.coverage_notes, *reader_date_notes]]
    for number, update in enumerate(numbered, 1):
        story.append(p(f"{number}. " + update.title, "label", True))
        story += citations(update.citations, imported=update.imported_from_pdf)
    if report.route_corrections:
        story += [p("ACCESS / ROUTE CORRECTIONS", "label", True), table(["SOURCE", "CORRECTION"], [[p(a, "cell"), p(b, "cell")] for a, b in report.route_corrections], [151, WIDTH - 151])]
    if report.glossary:
        story += [PageBreak(), banner("Appendix C - Glossary", "glossary"), table(["TERM", "DEFINITION"], [[p(a, "cell", True), p(b, "cell")] for a, b in report.glossary], [151, WIDTH - 151])]
    try:
        def deterministic_canvas(*args, **kwargs):
            kwargs["invariant"] = 1
            return Canvas(*args, **kwargs)
        document.multiBuild(story, canvasmaker=deterministic_canvas)
        atomic_write_bytes(Path(output), buffer.getvalue())
    except Exception as exc:
        raise GenerationError("IAA CSC PDF generation failed") from exc
