"""Team Planning's PDF export: a plan-by-plan list of jobs, in Nscale's look (A4 landscape).

build_pdf(groups, heading=…, sub=…, counts=…) → the PDF's bytes. Each group is a plan
{ref, title, project, status, due, jobs: [{priority, title, description, people, start, end, duration,
status, progress, subs, subsDone, recur}]}. The Nscale wordmark is drawn from static/nscale-wordmark.svg.
"""
import io
import re
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

NAVY = colors.HexColor("#040E27")
TEXT2 = colors.HexColor("#3B404E")
MUTED = colors.HexColor("#6B7080")
BLUE = colors.HexColor("#0F41F3")
SURFACE = colors.HexColor("#E1EAF9")
PAGE = colors.HexColor("#F3F5FB")
LINE = colors.HexColor("#D9E1F2")
RED = colors.HexColor("#B42318")
STATUS = {   # badge fill, text
    "To do": ("#ECEEF3", "#3B404E"), "In progress": ("#E1EAF9", "#0F41F3"),
    "Blocked": ("#FDECEC", "#B42318"), "Done": ("#D6FAEE", "#006B4A"),
}
WORDMARK = Path(__file__).parent / "static" / "nscale-wordmark.svg"

PAGE_SIZE = landscape(A4)
MARGIN = 14 * mm


def _style(name, **kw) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=kw.pop("font", "Helvetica"), fontSize=kw.pop("size", 9), leading=kw.pop("leading", 12),
                          textColor=kw.pop("color", NAVY), **kw)


S_TITLE = _style("title", font="Helvetica-Bold", size=20, leading=24)
S_SUB = _style("sub", size=10, leading=14, color=TEXT2)
S_PLAN = _style("plan", font="Helvetica-Bold", size=11.5, leading=15)
S_PLANSUB = _style("plansub", size=8.5, leading=11, color=TEXT2)
S_CELL = _style("cell", size=9, leading=11.5)
S_JOB = _style("job", font="Helvetica-Bold", size=9, leading=11.5)
S_SMALL = _style("small", size=8, leading=10, color=MUTED)
S_HEAD = _style("head", font="Helvetica-Bold", size=7.5, leading=9, color=TEXT2)
S_EMPTY = _style("empty", size=11, leading=15, color=TEXT2)


# ---------- The wordmark, from its SVG (paths using M L H V C Z, absolute) ----------

def _wordmark_paths() -> list[tuple[str, float]]:
    try:
        svg = WORDMARK.read_text()
    except OSError:
        return []
    return [(m.group(1), float(m.group(2) or 1)) for m in re.finditer(r'<path\s+d="([^"]+)"(?:\s+opacity="([\d.]+)")?', svg)]


def draw_wordmark(c, x: float, y: float, height: float, fill=NAVY) -> float:
    """Draws the Nscale wordmark with its bottom-left at (x, y); returns its width."""
    scale = height / 20.0
    for d, opacity in _wordmark_paths():
        p = c.beginPath()
        toks = re.findall(r"[MLHVCZ]|-?\d*\.?\d+(?:e-?\d+)?", d)
        i, cmd, cx, cy = 0, "", 0.0, 0.0
        num = lambda k: float(toks[k])   # noqa: E731
        X = lambda v: x + v * scale       # noqa: E731
        Y = lambda v: y + (20 - v) * scale   # noqa: E731
        while i < len(toks):
            t = toks[i]
            if t in "MLHVCZ":
                cmd, i = t, i + 1
                if t == "Z":
                    p.close()
                continue
            if cmd == "M":
                cx, cy = num(i), num(i + 1); p.moveTo(X(cx), Y(cy)); i += 2; cmd = "L"
            elif cmd == "L":
                cx, cy = num(i), num(i + 1); p.lineTo(X(cx), Y(cy)); i += 2
            elif cmd == "H":
                cx = num(i); p.lineTo(X(cx), Y(cy)); i += 1
            elif cmd == "V":
                cy = num(i); p.lineTo(X(cx), Y(cy)); i += 1
            elif cmd == "C":
                x1, y1, x2, y2, cx, cy = (num(i + k) for k in range(6))
                p.curveTo(X(x1), Y(y1), X(x2), Y(y2), X(cx), Y(cy)); i += 6
            else:
                i += 1
        c.saveState()
        c.setFillColor(fill)
        c.setFillAlpha(opacity)
        c.drawPath(p, stroke=0, fill=1)
        c.restoreState()
    return 178 * scale


# ---------- Page furniture: a header band on page 1, a footer with page numbers on every page ----------

class _Numbered(rl_canvas.Canvas):
    """Holds each page until the end, so the footer can say "Page 2 of 5"."""
    def __init__(self, *a, footer: str = "", **kw):
        super().__init__(*a, **kw)
        self._pages, self._footer = [], footer

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        n = len(self._pages)
        for st in self._pages:
            self.__dict__.update(st)
            w, _ = PAGE_SIZE
            self.setStrokeColor(LINE); self.setLineWidth(0.6)
            self.line(MARGIN, 10 * mm, w - MARGIN, 10 * mm)
            self.setFont("Helvetica", 7.5); self.setFillColor(MUTED)
            self.drawString(MARGIN, 6.5 * mm, self._footer)
            self.drawRightString(w - MARGIN, 6.5 * mm, f"Page {self._pageNumber} of {n}")
            if self._pageNumber > 1:   # a small wordmark at the top of later pages
                draw_wordmark(self, MARGIN, PAGE_SIZE[1] - 9 * mm, 3.2 * mm)
            super().showPage()
        super().save()


def _first_page(c, doc):
    w, h = PAGE_SIZE
    c.saveState()
    c.setFillColor(PAGE); c.rect(0, h - 30 * mm, w, 30 * mm, stroke=0, fill=1)
    c.setFillColor(BLUE); c.rect(0, h - 30 * mm, w, 1.4 * mm, stroke=0, fill=1)
    ww = draw_wordmark(c, MARGIN, h - 17 * mm, 5.2 * mm)
    c.setStrokeColor(colors.HexColor("#C1CFF7")); c.setLineWidth(0.8)
    c.line(MARGIN + ww + 5 * mm, h - 17.5 * mm, MARGIN + ww + 5 * mm, h - 11 * mm)
    c.setFont("Helvetica-Bold", 10.5); c.setFillColor(TEXT2)
    c.drawString(MARGIN + ww + 9 * mm, h - 15.6 * mm, "Client Platforms · Team Planning")
    c.restoreState()


def _later_page(c, doc):
    pass


# ---------- The content ----------

def fmt_day(s: str) -> str:
    try:
        d = date.fromisoformat(s)
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {d.strftime('%b %Y')}"


def fmt_days(n) -> str:
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        return ""
    if not v:
        return ""
    s = str(int(v)) if v == int(v) else f"{v:g}"
    return f"{s} day" + ("" if v == 1 else "s")


def _badge(text: str, fill: str, fg: str) -> Table:
    from reportlab.pdfbase.pdfmetrics import stringWidth
    t = Table([[Paragraph(escape(text), _style("b", font="Helvetica-Bold", size=7.5, leading=9, color=colors.HexColor(fg)))]],
              colWidths=[stringWidth(text, "Helvetica-Bold", 7.5) + 12], hAlign="LEFT")
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(fill)), ("ROUNDEDCORNERS", [5, 5, 5, 5]),
                           ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                           ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return t


COLS = [10 * mm, 104 * mm, 56 * mm, 26 * mm, 26 * mm, 20 * mm, 27 * mm]   # 269 mm: A4 landscape less the margins


def _job_rows(jobs: list[dict], today: str) -> list[list]:
    rows = []
    for j in jobs:
        done = j.get("status") == "Done"
        late = not done and j.get("end") and j["end"] < today
        title = f'<font name="Helvetica-Bold">{escape(j.get("title") or "")}</font>'
        bits = []
        if (j.get("description") or "").strip():
            first = re.sub(r"\s+", " ", j["description"]).strip()
            bits.append(escape(first[:180] + ("…" if len(first) > 180 else "")))
        extra = []
        if j.get("subs"):
            extra.append(f'{j.get("subsDone", 0)} of {len(j["subs"])} sub jobs done')
        if j.get("recur"):
            extra.append("Repeats")
        if j.get("progress") and not done:
            extra.append(f'{j["progress"]}%')
        job = [Paragraph(title, S_CELL)]
        if bits:
            job.append(Paragraph(bits[0], S_SMALL))
        if extra:
            job.append(Paragraph(" · ".join(escape(x) for x in extra), S_SMALL))
        people = j.get("people") or []
        who = Paragraph(escape(", ".join(p.get("name") or p.get("email", "") for p in people)) if people
                        else '<font color="#B42318">Unassigned</font>', S_CELL)
        end = fmt_day(j.get("end", ""))
        rows.append([
            Paragraph(str(j.get("priority") or ""), _style("n", size=8.5, color=MUTED)),
            job, who,
            Paragraph(fmt_day(j.get("start", "")) or '<font color="#6B7080"><i>Not scheduled</i></font>', S_CELL),
            Paragraph(f'<font color="#B42318">{end}</font>' if late else end, S_CELL),
            Paragraph(fmt_days(j.get("duration")) if j.get("start") else "", S_CELL),
            _badge("Overdue" if late else j.get("status") or "To do", *(("#FDECEC", "#B42318") if late else STATUS.get(j.get("status"), STATUS["To do"]))),
        ])
    return rows


def _plan_block(p: dict, today: str) -> list:
    """One table per plan: its heading and the column headings repeat at the top of each page it runs onto."""
    facts = [x for x in (p.get("project"), p.get("status"), p.get("due") and "Due " + fmt_day(p["due"]),
                         f'{len(p["jobs"])} job' + ("" if len(p["jobs"]) == 1 else "s")) if x]
    head = Table([[Paragraph(f'<font color="#0F41F3">{escape(p.get("ref") or "")}</font>&nbsp;&nbsp;{escape(p.get("title") or "")}', S_PLAN),
                   Paragraph(escape(" · ".join(facts)), _style("pf", size=8.5, leading=11, color=TEXT2, alignment=2))]],
                 colWidths=[sum(COLS) * 0.6 - 12, sum(COLS) * 0.4 - 12])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    hdr = [Paragraph(t, S_HEAD) for t in ("#", "JOB", "WHO", "START", "END", "LENGTH", "STATUS")]
    body = _job_rows(p["jobs"], today)
    t = Table([[head] + [""] * (len(COLS) - 1), hdr] + body, colWidths=COLS, repeatRows=2, hAlign="LEFT")
    st = [("SPAN", (0, 0), (-1, 0)), ("BACKGROUND", (0, 0), (-1, 0), SURFACE), ("ROUNDEDCORNERS", [6, 6, 0, 0]),
          ("TOPPADDING", (0, 0), (-1, 0), 7), ("BOTTOMPADDING", (0, 0), (-1, 0), 7), ("LEFTPADDING", (0, 0), (-1, 0), 8),
          ("VALIGN", (0, 1), (-1, -1), "TOP"), ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
          ("TOPPADDING", (0, 1), (-1, -1), 5), ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
          ("LEFTPADDING", (0, 1), (-1, -1), 6), ("RIGHTPADDING", (0, 1), (-1, -1), 6),
          ("BOX", (0, 0), (-1, -1), 0.5, LINE)]
    for i in range(3, len(body) + 2, 2):
        st.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FAFBFE")))
    t.setStyle(TableStyle(st))
    return [t, Spacer(1, 7 * mm)]


def _summary(counts: list[tuple[str, int]]) -> Table:
    cells = []
    for label, n in counts:
        cells.append([Paragraph(f"{n:,}", _style("cn", font="Helvetica-Bold", size=16, leading=19)),
                      Paragraph(escape(label), _style("cl", size=8, leading=10, color=TEXT2))])
    if not cells:
        return Spacer(1, 1)
    w = sum(COLS) / max(len(cells), 1)
    t = Table([[c for c in cells]], colWidths=[w] * len(cells), hAlign="LEFT")
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
                           ("BACKGROUND", (0, 0), (-1, -1), colors.white), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                           ("ROUNDEDCORNERS", [6, 6, 6, 6])]))
    return t


def build_pdf(groups: list[dict], heading: str, sub: str, counts: list[tuple[str, int]], footer: str, today: str | None = None) -> bytes:
    today = today or date.today().isoformat()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=PAGE_SIZE, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=14 * mm, bottomMargin=16 * mm,
                            title=heading, author="Nscale Client Platforms", subject="Team Planning", creator="Nscale Client Platforms")
    story = [Spacer(1, 24 * mm), Paragraph(escape(heading), S_TITLE), Spacer(1, 2 * mm), Paragraph(escape(sub), S_SUB),
             Spacer(1, 6 * mm), _summary(counts), Spacer(1, 8 * mm)]
    if not groups:
        story.append(Paragraph("No jobs match.", S_EMPTY))
    for p in groups:
        story += _plan_block(p, today)
    doc.build(story, onFirstPage=_first_page, onLaterPages=_later_page,
              canvasmaker=lambda *a, **kw: _Numbered(*a, footer=footer, **kw))
    return buf.getvalue()
