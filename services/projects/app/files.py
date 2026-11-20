"""Files kept with a record (the Document Library's documents), and editing them in the page.

Each upload or save is a new version of the record's file (item_files, in the project database);
the latest is the file, older ones stay to download or restore (the newest MAX_VERSIONS are kept).
The record's own `file` field holds the latest version's details, so lists and search see them.

Editing in the page:
    CSV, TSV, text, Markdown, JSON …   the page edits the text and uploads it as the next version
    Word (.docx)    as paragraphs and tables (docx_blocks / docx_save). Only what you change is
                    rewritten, so pictures, headers, page layout and untouched text stay as they were.
    Excel (.xlsx)   as cells (xlsx_sheets / xlsx_save). Workbooks with charts or pictures aren't
                    edited here, since saving them that way would drop those.
Anything else (PDF, PowerPoint, pictures …) is kept and downloaded; PDFs and pictures can be viewed.
"""
import copy
import io
import re
import zipfile
from datetime import date, datetime

MAX_BYTES = 25 * 1024 * 1024
MAX_VERSIONS = 20
MAX_CELLS_ROWS, MAX_CELLS_COLS = 2000, 60

TEXT_EXT = {"csv", "tsv", "txt", "md", "markdown", "json", "xml", "yaml", "yml", "log", "ini", "conf", "ps1", "sh", "sql"}
# Shown in the page (not downloaded) only for these, always with this type, whatever the upload said.
INLINE = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
          "gif": "image/gif", "webp": "image/webp", "txt": "text/plain; charset=utf-8"}
TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv; charset=utf-8", "tsv": "text/tab-separated-values; charset=utf-8",
    "md": "text/markdown; charset=utf-8", "json": "application/json",
    **INLINE,
}


class FileError(ValueError):
    pass


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def clean_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "_", (name or "").strip()).strip(". ") or "document"
    return name[-180:]


def type_of(name: str) -> str:
    return TYPES.get(ext_of(name), "application/octet-stream")


def kind_of(name: str, data: bytes | None = None) -> str:
    """How the page can open it: text, docx, xlsx, pdf, image, or file (download only)."""
    e = ext_of(name)
    if e in TEXT_EXT:
        return "text"
    if e == "docx":
        return "docx"
    if e == "xlsx":
        return "xlsx"
    if e == "pdf":
        return "pdf"
    if e in ("png", "jpg", "jpeg", "gif", "webp"):
        return "image"
    return "file"


def check(name: str, data: bytes) -> None:
    if not data:
        raise FileError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise FileError(f"Files can be at most {MAX_BYTES // (1024 * 1024)} MB.")
    if ext_of(name) in ("docx", "xlsx", "pptx") and not zipfile.is_zipfile(io.BytesIO(data)):
        raise FileError(f"That isn't a readable .{ext_of(name)} file.")


# ---------- Word ----------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _runs_of(p) -> list[dict]:
    """A paragraph's text as runs with bold, italic and underline (hyperlinked text included)."""
    from docx.text.run import Run
    out = []
    for r in p._p.iter(W + "r"):
        run = Run(r, p)
        t = run.text
        if not t:
            continue
        f = {"t": t, "b": bool(run.bold), "i": bool(run.italic), "u": bool(run.underline)}
        if out and all(out[-1][k] == f[k] for k in "biu"):
            out[-1]["t"] += t
        else:
            out.append(f)
    return out


def _locked(p) -> bool:
    """Paragraphs with pictures, fields, equations or text boxes are kept exactly as they are."""
    el = p._p
    return any(el.iter(W + "drawing")) or any(el.iter(W + "pict")) or any(el.iter(W + "object")) or \
        any(el.iter(W + "fldChar")) or any(el.iter(W + "fldSimple")) or \
        any(el.iter("{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath"))


def _is_list(p) -> bool:
    ppr = p._p.pPr
    return (ppr is not None and ppr.numPr is not None) or (p.style is not None and (p.style.name or "").startswith("List"))


def _cell_text(tc) -> str:
    return "\n".join("".join(t.text or "" for t in par.iter(W + "t")) for par in tc.iter(W + "p"))


def docx_blocks(data: bytes) -> dict:
    """The document's body as blocks the page edits: paragraphs (with runs) and tables."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    d = docx.Document(io.BytesIO(data))
    blocks = []
    for i, el in enumerate(d.element.body.iterchildren()):
        tag = el.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(el, d._body)
            style = p.style.name if p.style is not None else "Normal"
            if _locked(p):
                blocks.append({"id": i, "kind": "locked", "label": _locked_label(p), "text": p.text[:200]})
            else:
                blocks.append({"id": i, "kind": "p", "style": style, "list": _is_list(p), "runs": _runs_of(p)})
        elif tag == "tbl":
            t = Table(el, d._body)
            rows = [[_cell_text(tc) for tc in tr.iter(W + "tc") if tc.getparent() is tr] for tr in t._tbl.iter(W + "tr")]
            nested = any(True for tc in el.iter(W + "tc") for _ in tc.iter(W + "tbl"))
            blocks.append({"id": i, "kind": "locked", "label": "Table with tables inside it"} if nested else
                          {"id": i, "kind": "table", "rows": rows})
        elif tag != "sectPr":
            blocks.append({"id": i, "kind": "locked", "label": "Content that can't be edited here"})
    styles = [s.name for s in d.styles if s.type == 1 and s.name]   # paragraph styles
    common = [s for s in ["Normal", "Title", "Heading 1", "Heading 2", "Heading 3", "List Bullet", "List Number", "Quote"] if s in styles]
    return {"blocks": blocks, "styles": common + sorted(set(b["style"] for b in blocks if b["kind"] == "p") - set(common))}


def _locked_label(p) -> str:
    el = p._p
    if any(el.iter(W + "drawing")) or any(el.iter(W + "pict")):
        return "Picture"
    if any(el.iter("{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath")):
        return "Equation"
    if any(el.iter(W + "fldChar")) or any(el.iter(W + "fldSimple")):
        return "Field (e.g. contents or page number)"
    return "Content that can't be edited here"


def _set_runs(p, runs: list[dict]) -> None:
    """Replace a paragraph's text, keeping its first run's formatting as the base for each new run."""
    from docx.text.run import Run
    el = p._p
    first = next(el.iter(W + "r"), None)
    base = copy.deepcopy(first.rPr) if first is not None and first.rPr is not None else None
    for child in list(el):
        if child.tag in (W + "r", W + "hyperlink", W + "ins", W + "del", W + "smartTag"):
            el.remove(child)
    for r in runs:
        t = str(r.get("t", ""))
        if not t:
            continue
        new = el.add_r()
        if base is not None:
            new.insert(0, copy.deepcopy(base))
        run = Run(new, p)
        run.text = t
        run.bold, run.italic, run.underline = bool(r.get("b")) or None, bool(r.get("i")) or None, bool(r.get("u")) or None


def _set_cell(tc, text: str) -> None:
    from docx.table import _Cell
    cell = _Cell(tc, None)
    paras = cell.paragraphs
    lines = str(text).split("\n")
    first = paras[0]
    for extra in paras[1:]:
        extra._p.getparent().remove(extra._p)
    _set_runs(first, [{"t": lines[0]}])
    prev = first._p
    for line in lines[1:]:
        new = copy.deepcopy(first._p)
        prev.addnext(new)
        from docx.text.paragraph import Paragraph
        _set_runs(Paragraph(new, None), [{"t": line}])
        prev = new


def docx_save(data: bytes, blocks: list) -> bytes:
    """The document with the page's blocks: in that order, changed ones rewritten, missing ones removed."""
    import docx
    from docx.text.paragraph import Paragraph
    d = docx.Document(io.BytesIO(data))
    body = d.element.body
    old = {i: el for i, el in enumerate(body.iterchildren())}
    before = docx_blocks(data)["blocks"]
    was = {b["id"]: b for b in before}
    sect = body.find(W + "sectPr")
    styles = {s.name for s in d.styles if s.type == 1 and s.name}
    out = []
    if not isinstance(blocks, list) or len(blocks) > 20000:
        raise FileError("That isn't a document to save.")
    for b in blocks:
        if not isinstance(b, dict):
            continue
        bid = b.get("id")
        kind = b.get("kind")
        if isinstance(bid, int) and bid in old and bid in was:
            el = old.pop(bid)
            w = was[bid]
            if w["kind"] == "p" and kind == "p":
                p = Paragraph(el, d._body)
                if b.get("runs") != w["runs"]:
                    _set_runs(p, b.get("runs") or [])
                if b.get("style") and b["style"] != w["style"] and b["style"] in styles:
                    p.style = d.styles[b["style"]]
            elif w["kind"] == "table" and kind == "table":
                _save_table(el, w["rows"], b.get("rows") or [])
            out.append(el)
        elif kind == "p":
            like = old.get(b.get("like")) if isinstance(b.get("like"), int) else None
            like = like if like is not None else next((e for e in reversed(out) if e.tag == W + "p"), None)
            from docx.oxml import OxmlElement
            el = OxmlElement("w:p")
            if like is not None and like.tag == W + "p" and like.pPr is not None:
                el.insert(0, copy.deepcopy(like.pPr))
            p = Paragraph(el, d._body)
            _set_runs(p, b.get("runs") or [])
            if b.get("style") in styles:
                p.style = d.styles[b["style"]]
            out.append(el)
        elif kind == "table" and isinstance(b.get("rows"), list):
            out.append(_new_table(d, b["rows"]))
    # Everything goes back in the page's order; what the page left out is removed. The section settings stay last.
    for el in list(body):
        body.remove(el)
    for el in out:
        body.append(el)
    if sect is not None:
        body.append(sect)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _save_table(tbl, before: list, rows: list) -> None:
    trs = [tr for tr in tbl.iter(W + "tr") if tr.getparent() is tbl]
    rows = [[str(c) for c in r] if isinstance(r, list) else [] for r in rows][:MAX_CELLS_ROWS]
    # Rows added at the end copy the last row; rows taken off the end go.
    while len(trs) < len(rows) and trs:
        new = copy.deepcopy(trs[-1])
        trs[-1].addnext(new)
        trs.append(new)
        before = before + [[""] * len([tc for tc in new.iter(W + "tc") if tc.getparent() is new])]
    for tr in trs[len(rows):]:
        tbl.remove(tr)
    for r, tr in enumerate(trs[:len(rows)]):
        tcs = [tc for tc in tr.iter(W + "tc") if tc.getparent() is tr]
        for c, tc in enumerate(tcs):
            if c < len(rows[r]) and (r >= len(before) or c >= len(before[r]) or rows[r][c] != before[r][c]):
                _set_cell(tc, rows[r][c])


def _new_table(d, rows: list):
    rows = [[str(c) for c in r] for r in rows if isinstance(r, list)][:MAX_CELLS_ROWS] or [[""]]
    cols = max(1, max(len(r) for r in rows))
    t = d.add_table(rows=len(rows), cols=cols)
    try:
        t.style = d.styles["Table Grid"]
    except KeyError:
        pass
    for r, row in enumerate(rows):
        for c in range(cols):
            t.cell(r, c).text = row[c] if c < len(row) else ""
    el = t._tbl
    el.getparent().remove(el)
    return el


# ---------- Excel ----------

def _has_drawings(data: bytes) -> bool:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return any(n.startswith(("xl/drawings/", "xl/charts/", "xl/media/", "xl/pivotTables/")) for n in z.namelist())


def _shown(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def xlsx_sheets(data: bytes) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data))
    sheets = []
    for ws in wb.worksheets:
        rows, cols = min(ws.max_row, MAX_CELLS_ROWS), min(ws.max_column, MAX_CELLS_COLS)
        grid = [[_shown(c.value) for c in row] for row in ws.iter_rows(min_row=1, max_row=rows, max_col=cols)]
        sheets.append({"name": ws.title, "rows": grid, "truncated": ws.max_row > rows or ws.max_column > cols})
    locked = _has_drawings(data)
    return {"sheets": sheets, "readOnly": locked,
            "why": "This workbook has charts, pictures or pivot tables, which saving here would drop. Download it to change it." if locked else ""}


NUM = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def _value(text: str, old):
    text = str(text)
    if text == "":
        return None
    if text.startswith("="):
        return text
    if NUM.match(text.replace(",", "")) and not re.match(r"^0\d", text):
        n = float(text.replace(",", ""))
        return int(n) if n.is_integer() and "." not in text and "e" not in text.lower() else n
    if text.upper() in ("TRUE", "FALSE"):
        return text.upper() == "TRUE"
    if isinstance(old, (date, datetime)) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M") if " " in text else datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            pass
    return text


def xlsx_save(data: bytes, edits: list) -> bytes:
    """The workbook with the page's cell changes: [{sheet, r, c, v}] (rows and columns from 0)."""
    import openpyxl
    if _has_drawings(data):
        raise FileError("This workbook has charts, pictures or pivot tables, which saving here would drop. Download it to change it.")
    if not isinstance(edits, list) or len(edits) > 200000:
        raise FileError("Those aren't changes to save.")
    wb = openpyxl.load_workbook(io.BytesIO(data))
    for e in edits:
        if not isinstance(e, dict) or e.get("sheet") not in wb.sheetnames:
            continue
        r, c = e.get("r"), e.get("c")
        if not (isinstance(r, int) and isinstance(c, int) and 0 <= r < 1048576 and 0 <= c < 16384):
            continue
        cell = wb[e["sheet"]].cell(row=r + 1, column=c + 1)
        v = _value(e.get("v", ""), cell.value)
        cell.value = v
        if isinstance(v, datetime) and not cell.is_date:
            cell.number_format = "yyyy-mm-dd" if (v.hour, v.minute) == (0, 0) else "yyyy-mm-dd hh:mm"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
