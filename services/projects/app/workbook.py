"""A project as an Excel workbook, and back.

Export writes one sheet for the project's details and one per section (Risk Register, Action Log ...),
a column per field, a row per record. Import reads the same layout: a blank template filled in, an
export edited, or a workbook from somewhere else laid out the same way. Sheets and columns are matched
by their name (or the section's key), ignoring case; anything it doesn't recognise is left out.

Cells that hold a list:
    people (Made by, Attendees)       comma-separated names
    Updates                           one per line: 2026-11-20 | Name | what happened
    a meeting's Actions               one per line: what to do | who | 2026-12-01 | Open

Attached files (Document Library) aren't in the workbook; their other details are. Action Log
records that came from a meeting are left out of the export, because importing the meeting makes them.
"""
import io
import re
from datetime import date, datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import modules

PROJECT_SHEET = "Project"
README_SHEET = "Read me"
PROJECT_FIELDS = [("name", "Name"), ("description", "Description"), ("status", "Status"),
                  ("currency", "Currency"), ("approvedBudget", "Approved budget")]
MAX_ROWS_PER_SHEET = 2000
NAVY, GREY = "0B1B3F", "ECEEF3"
WIDTH = {"textarea": 48, "updates": 56, "actions": 56, "users": 30, "date": 13, "scale": 11, "rag": 10,
         "number": 11, "currency": 13, "computed": 14}


class WorkbookError(ValueError):
    pass


def columns(mod: dict) -> list[dict]:
    """The fields that go in a section's sheet: everything but attached files and hidden fields."""
    return [f for f in mod["fields"] if f["type"] != "file" and not f.get("hidden")]


def sheet_name(mod: dict) -> str:
    return re.sub(r"[\[\]:*?/\\]", "-", mod["label"])[:31]


# ---------- Export ----------

def _cell(f: dict, data: dict):
    v = data.get(f["key"])
    t = f["type"]
    if t == "computed":
        if f["key"] == "score" and v:
            return f"{modules.risk_rating(v)} ({v})"
        return v if v != "" else None
    if t == "users":
        return ", ".join(v or [])
    if t == "updates":
        return "\n".join(" | ".join([u.get("date", ""), u.get("user", ""), u.get("notes", "")]) for u in v or [])
    if t == "actions":
        return "\n".join(" | ".join([a.get("description", ""), a.get("assignedTo", ""), a.get("dueDate", ""),
                                     a.get("status", "")]) for a in v or [])
    if t == "date" and isinstance(v, str) and modules.DATE.match(v):
        return date.fromisoformat(v)
    if t == "scale" and isinstance(v, str) and v.isdigit():
        return int(v)
    return None if v in (None, "") else v


def _header(ws, labels: list[str], computed: set[int] = frozenset()) -> None:
    ws.append(labels)
    for i, c in enumerate(ws[1], start=1):
        c.font = Font(bold=True, color="FFFFFF" if i not in computed else "555555")
        c.fill = PatternFill("solid", fgColor=NAVY if i not in computed else GREY)
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 30


def _readme(ws, project_name: str) -> None:
    rows = [
        ["Nscale project workbook" + (f": {project_name}" if project_name else "")],
        [],
        ["Fill in or change the sheets, then use Import in a project to add the rows to it (or Import project on the"
         " Projects page to make a new project). Every row becomes a new record with its own reference."],
        ["Grey columns (Ref, Risk Rating, Variance) are worked out by the site and are ignored when importing."],
        ["People: comma-separated names. Updates: one per line, as  date | name | note."
         " Meeting actions: one per line, as  description | assigned to | due date | status."],
        ["Dates as dates, or as YYYY-MM-DD or DD/MM/YYYY. Columns marked * must be filled in."
         " Files attached in the Document Library aren't included."],
        [],
        ["Sheet", "Required columns"],
    ]
    for mod in modules.MODULES:
        rows.append([sheet_name(mod), ", ".join(f["label"] for f in columns(mod) if f.get("required"))])
    for r in rows:
        ws.append(r)
    ws["A1"].font = Font(bold=True, size=14, color=NAVY)
    ws["A8"].font = ws["B8"].font = Font(bold=True)
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 70


def export(project: dict | None, items: list[dict]) -> bytes:
    """The workbook for a project (or a blank template when project is None)."""
    wb = Workbook()
    _readme(wb.active, (project or {}).get("name", ""))
    wb.active.title = README_SHEET

    ws = wb.create_sheet(PROJECT_SHEET)
    _header(ws, ["Field", "Value"])
    for k, label in PROJECT_FIELDS:
        ws.append([label + (" *" if k == "name" else ""), (project or {}).get(k, "") if project else ""])
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 20, 60
    for k, opts in (("status", modules.PROJECT_STATUSES), ("currency", modules.CURRENCIES)):
        row = 2 + [x for x, _ in PROJECT_FIELDS].index(k)
        dv = DataValidation(type="list", formula1='"' + ",".join(opts) + '"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"B{row}")

    by_mod: dict[str, list[dict]] = {}
    for it in items:
        if it["module"] == "actions" and (it.get("source") or {}).get("meeting"):
            continue
        by_mod.setdefault(it["module"], []).append(it)

    for mod in modules.MODULES:
        cols = columns(mod)
        ws = wb.create_sheet(sheet_name(mod))
        labels = ["Ref"] + [f["label"] + (" *" if f.get("required") else "") for f in cols]
        computed = {1} | {i + 2 for i, f in enumerate(cols) if f["type"] == "computed"}
        _header(ws, labels, computed)
        rows = by_mod.get(mod["key"], [])
        if mod["key"] == "milestones":
            rows.sort(key=lambda i: (i["data"].get("order") or 0))
        for it in rows:
            ws.append([it.get("ref") or None] + [_cell(f, it["data"]) for f in cols])
        last = max(len(rows) + 1, 200)
        ws.column_dimensions["A"].width = 10
        for i, f in enumerate(cols, start=2):
            letter = get_column_letter(i)
            ws.column_dimensions[letter].width = WIDTH.get(f["type"], 22)
            if f["type"] == "date":
                for r in range(2, len(rows) + 2):
                    ws[f"{letter}{r}"].number_format = "yyyy-mm-dd"
            if f["type"] in ("textarea", "updates", "actions"):
                for r in range(2, len(rows) + 2):
                    ws[f"{letter}{r}"].alignment = Alignment(wrap_text=True, vertical="top")
            opts = f.get("options")
            if f["type"] in ("select", "scale", "rag") and opts and len(",".join(opts)) < 250 and "," not in "".join(opts):
                dv = DataValidation(type="list", formula1='"' + ",".join(opts) + '"', allow_blank=True)
                ws.add_data_validation(dv)
                dv.add(f"{letter}2:{letter}{last}")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- Import ----------

def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower().replace("*", ""))


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()


def _date(v) -> str:
    s = _text(v)
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", s)   # UK style
    if m:
        try:
            return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        except ValueError:
            return s
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T].*", s)
    return m[1] if m else s


def _option(v: str, opts: list[str]) -> str:
    for o in opts:
        if o.lower() == v.lower():
            return o
    return v


def _value(f: dict, v):
    t = f["type"]
    if t == "date":
        return _date(v)
    if t == "users":
        return _text(v)   # sanitize splits on commas
    if t == "updates":
        rows = []
        for line in _text(v).splitlines():
            parts = [p.strip() for p in line.split("|")]
            d = _date(parts[0]) if parts else ""
            if len(parts) >= 3 and modules.DATE.match(d):
                rows.append({"date": d, "user": parts[1], "notes": " | ".join(parts[2:])})
            elif line.strip() and rows:   # a note that ran onto another line
                rows[-1]["notes"] += "\n" + line.strip()
            elif line.strip():
                rows.append({"date": modules.today(), "user": "", "notes": line.strip()})
        return rows
    if t == "actions":
        rows = []
        for line in _text(v).splitlines():
            parts = [p.strip() for p in line.split("|")] + ["", "", ""]
            if parts[0]:
                rows.append({"description": parts[0], "assignedTo": parts[1], "dueDate": _date(parts[2]),
                             "status": _option(parts[3], f.get("options", []))})
        return rows
    s = _text(v)
    if t in ("select", "scale", "rag"):
        return _option(s, f.get("options", []))
    return s


def read(data: bytes) -> dict:
    """{project: {...}, sections: {key: [(row number, raw record)]}, skipped: [sheet names]}.
    Raises WorkbookError if it isn't a workbook."""
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise WorkbookError("That isn't an Excel workbook (.xlsx).")
    by_sheet = {}
    for mod in modules.MODULES:
        by_sheet[_norm(sheet_name(mod))] = mod
        by_sheet[_norm(mod["label"])] = mod
        by_sheet[_norm(mod["key"])] = mod
    out = {"project": {}, "sections": {}, "skipped": []}
    try:
        for ws in wb.worksheets:
            n = _norm(ws.title)
            if n == _norm(README_SHEET):
                continue
            if n == _norm(PROJECT_SHEET):
                labels = {_norm(lbl): k for k, lbl in PROJECT_FIELDS} | {_norm(k): k for k, _ in PROJECT_FIELDS}
                for row in ws.iter_rows(min_row=1, max_col=2, values_only=True):
                    k = labels.get(_norm(row[0] if row else ""))
                    if k and len(row) > 1 and _text(row[1]):
                        out["project"][k] = _text(row[1])
                continue
            mod = by_sheet.get(n)
            if not mod:
                out["skipped"].append(ws.title)
                continue
            rows = ws.iter_rows(values_only=True)
            head = next(rows, None) or ()
            fields = {}
            for f in columns(mod):
                fields[_norm(f["label"])] = f
                fields[_norm(f["key"])] = f
            cols = [(i, fields.get(_norm(h))) for i, h in enumerate(head)]
            cols = [(i, f) for i, f in cols if f and f["type"] != "computed"]
            recs = out["sections"].setdefault(mod["key"], [])
            for r, row in enumerate(rows, start=2):
                if not any(_text(c) for c in row):
                    continue
                if len(recs) >= MAX_ROWS_PER_SHEET:
                    raise WorkbookError(f"{ws.title} has more than {MAX_ROWS_PER_SHEET} rows.")
                recs.append((r, {f["key"]: _value(f, row[i] if i < len(row) else None) for i, f in cols}))
    finally:
        wb.close()
    out["sections"] = {k: v for k, v in out["sections"].items() if v}
    return out


def check(parsed: dict) -> tuple[dict, list[str]]:
    """Every record checked the way the form checks it. ({key: [clean records]}, [problems])."""
    good, problems = {}, []
    for key, recs in parsed["sections"].items():
        mod = modules.BY_KEY[key]
        for r, raw in recs:
            try:
                good.setdefault(key, []).append(modules.sanitize(mod, raw))
            except modules.ValidationError as e:
                problems.append(f"{sheet_name(mod)}, row {r}: {e}")
    return good, problems
