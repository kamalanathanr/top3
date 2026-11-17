"""
Executive-style PDF of an events timeline (News page → ⬇ on a timeline).

The page sends the events it is currently showing (after the Local / National /
International / Online filters), so the PDF matches the screen exactly.
"""

import io
import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

PREPARED_BY = "kamalanathan.r@gmail.com"

NAVY = colors.HexColor("#1F2A44")
GOLD = colors.HexColor("#B8912F")
SLATE = colors.HexColor("#5B6475")
RULE = colors.HexColor("#D9DEE7")
ZEBRA = colors.HexColor("#F5F7FA")
MONTH_BG = colors.HexColor("#E9EDF4")

# Unicode font for ★ / ₹ / accents (macOS ships Arial Unicode); falls back to Helvetica
_FONT_DIR = "/System/Library/Fonts/Supplemental"
if os.path.exists(f"{_FONT_DIR}/Arial Unicode.ttf"):
    pdfmetrics.registerFont(TTFont("Body", f"{_FONT_DIR}/Arial Unicode.ttf"))
    pdfmetrics.registerFont(TTFont("BodyBold", f"{_FONT_DIR}/Arial Bold.ttf")
                            if os.path.exists(f"{_FONT_DIR}/Arial Bold.ttf") else TTFont("BodyBold", f"{_FONT_DIR}/Arial Unicode.ttf"))
    BODY, BOLD, STAR_OK = "Body", "BodyBold", True
else:
    BODY, BOLD, STAR_OK = "Helvetica", "Helvetica-Bold", False

S = {
    "title": ParagraphStyle("title", fontName=BOLD, fontSize=20, leading=24, textColor=NAVY),
    "sub": ParagraphStyle("sub", fontName=BODY, fontSize=9.5, leading=13, textColor=SLATE),
    "month": ParagraphStyle("month", fontName=BOLD, fontSize=9, leading=11, textColor=NAVY),
    "cell": ParagraphStyle("cell", fontName=BODY, fontSize=8.2, leading=10.4, textColor=colors.HexColor("#29292D")),
    "name": ParagraphStyle("name", fontName=BOLD, fontSize=8.8, leading=11, textColor=NAVY),
    "small": ParagraphStyle("small", fontName=BODY, fontSize=7.4, leading=9.4, textColor=SLATE),
    "date": ParagraphStyle("date", fontName=BOLD, fontSize=8.8, leading=10.6, textColor=NAVY),
    "stars": ParagraphStyle("stars", fontName=BODY, fontSize=8.5, leading=10, textColor=GOLD),
    "head": ParagraphStyle("head", fontName=BOLD, fontSize=7.6, leading=9, textColor=colors.white),
}


def _d(iso: str) -> date:
    return date.fromisoformat(iso[:10])


def _stars(n) -> str:
    n = max(1, min(5, int(n or 1)))
    return "★" * n + "☆" * (5 - n) if STAR_OK else f"{n}/5"


class _NumberedCanvas(rl_canvas.Canvas):
    """Two-pass canvas so the footer can say 'Page X of Y'."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total):
        w, _ = A4
        self.setStrokeColor(RULE)
        self.setLineWidth(0.6)
        self.line(16 * mm, 14 * mm, w - 16 * mm, 14 * mm)
        self.setFont(BODY, 7.5)
        self.setFillColor(SLATE)
        self.drawString(16 * mm, 9.5 * mm, f"Top3 · Upcoming events · Prepared by {PREPARED_BY}")
        self.drawRightString(w - 16 * mm, 9.5 * mm, f"Page {self._pageNumber} of {total}")


def _top_band(c, doc):
    """Navy band with a gold rule across the top of every page."""
    w, h = A4
    c.saveState()
    c.setFillColor(NAVY)
    c.rect(0, h - 9 * mm, w, 9 * mm, stroke=0, fill=1)
    c.setFillColor(GOLD)
    c.rect(0, h - 10.2 * mm, w, 1.2 * mm, stroke=0, fill=1)
    c.setFont(BOLD, 8)
    c.setFillColor(colors.white)
    c.drawString(16 * mm, h - 6 * mm, "TOP3 · NEWS & EVENTS")
    c.restoreState()


def build_events_pdf(tab: str, events: list, filters_label: str = "", timezone: str = "Asia/Kolkata") -> bytes:
    now = datetime.now(ZoneInfo(timezone))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=18 * mm, bottomMargin=20 * mm,
                            title=f"Upcoming events — {tab}", author=PREPARED_BY, subject="Upcoming events timeline")

    story = [
        Paragraph(f"Upcoming Events — {escape(tab)}", S["title"]),
        Spacer(1, 3),
        Paragraph(
            f"<b>{len(events)}</b> event{'s' if len(events) != 1 else ''}"
            + (f" &nbsp;·&nbsp; Filters: {escape(filters_label)}" if filters_label else "")
            + f"<br/>Generated {now:%d %b %Y, %I:%M %p} {now.tzname()} &nbsp;·&nbsp; Prepared by <font color='#1F2A44'><b>{PREPARED_BY}</b></font>",
            S["sub"]),
        Spacer(1, 4),
    ]
    # gold rule under the heading
    rule = Table([[""]], colWidths=[doc.width], rowHeights=[1.2])
    rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GOLD)]))
    story += [rule, Spacer(1, 8)]

    if not events:
        story.append(Paragraph("No events match the current filters.", S["cell"]))
    else:
        widths = [20 * mm, 66 * mm, 21 * mm, 30 * mm, 17 * mm, doc.width - 154 * mm]   # source ≈ 24 mm
        rows = [[Paragraph(h, S["head"]) for h in ("DATE", "EVENT", "TYPE", "LOCATION", "RATING", "SOURCE")]]
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
        ]
        month = None
        zebra = False
        for e in sorted(events, key=lambda x: (x.get("start_date") or "", -(x.get("stars") or 0))):
            start = _d(e["start_date"])
            m = f"{start:%B %Y}".upper()
            if m != month:
                month = m
                rows.append([Paragraph(m, S["month"])] + [""] * 5)
                r = len(rows) - 1
                style += [("SPAN", (0, r), (-1, r)), ("BACKGROUND", (0, r), (-1, r), MONTH_BG),
                          ("TOPPADDING", (0, r), (-1, r), 3), ("BOTTOMPADDING", (0, r), (-1, r), 3)]
                zebra = False
            end = _d(e.get("end_date") or e["start_date"])
            when = f"{start:%d %b}<br/><font size='7' color='#5B6475'>{start:%a}</font>"
            if end != start:
                when = f"{start:%d %b} –<br/>{end:%d %b}"
            name = escape(e.get("name") or "")
            if e.get("url"):
                name = f"<link href='{escape(e['url'])}'>{name}</link>"
            desc = escape(e.get("description") or "")
            rows.append([
                Paragraph(when, S["date"]),
                Paragraph(f"{name}" + (f"<br/><font name='{BODY}' size='7.4' color='#5B6475'>{desc}</font>" if desc else ""), S["name"]),
                Paragraph(escape(e.get("kind") or "—"), S["cell"]),
                Paragraph(escape(e.get("location") or "—"), S["cell"]),
                Paragraph(_stars(e.get("stars")), S["stars"]),
                Paragraph(escape(re.sub(r",\s*\d{4}-\d{2}-\d{2}$", "", e.get("source") or "—")), S["small"]),
            ])
            if zebra:
                style.append(("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), ZEBRA))
            zebra = not zebra

        table = Table(rows, colWidths=widths, repeatRows=1)
        table.setStyle(TableStyle(style))
        story.append(table)
        story += [Spacer(1, 8), Paragraph(
            "Rating = popularity / importance (★ 1–5) as shown by the rings on the News page timeline.", S["small"])]

    doc.build(story, onFirstPage=_top_band, onLaterPages=_top_band, canvasmaker=_NumberedCanvas)
    return buf.getvalue()
