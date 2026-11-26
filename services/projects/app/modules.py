"""The sections of a project, and the rules for their records.

modules.json holds each section's definition, taken from TechOps' Project Management workspace
(src/modules.js there): its fields, list columns, filters, statuses and standard records. The page
draws its lists and forms from these definitions; this module checks what comes back, works out
the computed fields, and builds the dashboard and the draft status report.

Field types: text, textarea, date, select, scale (1-5), rag, number, currency, url,
user (one person), users (several), updates (dated progress log), actions (meeting actions), computed,
file (an uploaded file with its versions; see files.py).
People are kept as plain names or emails.
"""
import json
import re
import uuid
from datetime import date, timedelta
from pathlib import Path

_DEF = json.loads((Path(__file__).parent / "modules.json").read_text())
PHASES: list[str] = _DEF["phases"]
MODULES: list[dict] = _DEF["modules"]
BY_KEY = {m["key"]: m for m in MODULES}
RAG = ["Green", "Amber", "Red"]
DONE_ACTION = ("Completed", "Cancelled")
PROJECT_STATUSES = ["active", "on hold", "completed", "archived"]
CURRENCIES = ["GBP", "EUR", "USD"]

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_TEXT, MAX_TEXTAREA, MAX_PEOPLE, MAX_ROWS = 500, 20000, 100, 200
MAX_AMOUNT = 1e12


class ValidationError(ValueError):
    pass


def risk_rating(score) -> str:
    """Likelihood × impact as a word: Critical 20+, High 12–19, Medium 6–11, Low below."""
    score = score or 0
    return "Critical" if score >= 20 else "High" if score >= 12 else "Medium" if score >= 6 else "Low"


def today() -> str:
    return date.today().isoformat()


def add_days(iso: str, days: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()


def _person(v) -> str:
    """People are names or emails as typed (or picked from the project's members)."""
    if isinstance(v, dict):
        v = v.get("name") or v.get("email") or ""
    return str(v or "").strip()[:200]


def _rid(v) -> str:
    return v if isinstance(v, str) and re.fullmatch(r"[\w-]{1,64}", v) else uuid.uuid4().hex


def sanitize(mod: dict, raw: dict, existing: dict | None = None) -> dict:
    """The record to store, from what the page sent. Raises ValidationError listing every problem."""
    raw = raw if isinstance(raw, dict) else {}
    data, errors = {}, []
    for f in mod["fields"]:
        k, t, label = f["key"], f["type"], f["label"]
        if t == "computed":
            continue
        if t == "file":   # set only by uploading (main.py's file routes), never from the form
            data[k] = (existing or {}).get(k) or None
            continue
        v = raw.get(k)
        if f.get("hidden") and v is None and existing:
            data[k] = existing.get(k, "")
            continue
        if f.get("readOnly"):
            data[k] = existing.get(k, "") if existing else (v if isinstance(v, str) and DATE.match(v) else
                                                            today() if f.get("defaultToday") else "")
            continue
        if t == "user":
            p = _person(v)
            if f.get("required") and not p:
                errors.append(f"{label} is required.")
            data[k] = p
            continue
        if t == "users":
            items = v if isinstance(v, list) else re.split(r"[,;\n]", v) if isinstance(v, str) else []
            seen, people = set(), []
            for p in map(_person, items):
                if p and p.lower() not in seen:
                    seen.add(p.lower())
                    people.append(p)
            if len(people) > MAX_PEOPLE:
                errors.append(f"{label} can list at most {MAX_PEOPLE} people.")
            if f.get("required") and not people:
                errors.append(f"{label} is required.")
            data[k] = people
            continue
        if t == "updates":
            rows = []
            for i, u in enumerate(v if isinstance(v, list) else []):
                notes = str((u or {}).get("notes", "")).strip()
                if not notes:
                    errors.append(f"Update {i + 1} needs a note.")
                    continue
                d = (u or {}).get("date")
                rows.append({"id": _rid(u.get("id")), "date": d if isinstance(d, str) and DATE.match(d) else today(),
                             "user": _person(u.get("user")), "notes": notes[:4000]})
            if len(rows) > MAX_ROWS:
                errors.append(f"{label} can hold at most {MAX_ROWS} entries.")
            data[k] = sorted(rows, key=lambda r: r["date"], reverse=True)
            continue
        if t == "actions":
            rows = []
            for i, a in enumerate(v if isinstance(v, list) else []):
                desc = str((a or {}).get("description", "")).strip()
                if not desc:
                    errors.append(f"Action {i + 1} needs a description.")
                    continue
                d = a.get("dueDate")
                rows.append({"id": _rid(a.get("id")), "description": desc[:2000], "assignedTo": _person(a.get("assignedTo")),
                             "dueDate": d if isinstance(d, str) and DATE.match(d) else "",
                             "status": a.get("status") if a.get("status") in f.get("options", []) else "Open"})
            if len(rows) > MAX_ROWS:
                errors.append(f"A meeting can have at most {MAX_ROWS} actions.")
            data[k] = rows
            continue
        if t in ("number", "currency"):
            text = "" if v is None else re.sub(r"[,\s£$€]", "", str(v))
            if text == "":
                if f.get("required"):
                    errors.append(f"{label} is required.")
                data[k] = ""
                continue
            try:
                n = float(text)
                if not (0 <= n <= MAX_AMOUNT):
                    raise ValueError
            except ValueError:
                errors.append(f"{label} must be a positive number.")
                data[k] = ""
                continue
            data[k] = round(n, 2) if t == "currency" else (int(n) if n == int(n) else n)
            continue

        value = "" if v is None or isinstance(v, (dict, list)) else str(v).strip()
        if not value and f.get("default"):
            value = f["default"]
        if t in ("text", "url") and len(value) > (2000 if t == "url" else MAX_TEXT):
            errors.append(f"{label} is too long.")
        if t == "textarea" and len(value) > MAX_TEXTAREA:
            errors.append(f"{label} is too long.")
        if t == "date" and value and not DATE.match(value):
            errors.append(f"{label} must be a date (YYYY-MM-DD).")
        if t == "url" and value and not re.match(r"^https?://[^\s]+$", value, re.I):
            errors.append(f"{label} must be a web link starting with https://")
        if t in ("select", "scale", "rag") and value and value not in f.get("options", []):
            errors.append(f"{label} has an invalid value.")
        if f.get("required") and not value:
            errors.append(f"{label} is required.")
        data[k] = value

    key = mod["key"]
    if key == "milestones" and data.get("startDate") and data.get("endDate") and data["startDate"] > data["endDate"]:
        errors.append("Start date must be on or before the end date.")
    if errors:
        raise ValidationError(" ".join(errors))
    if key == "risks":
        data["score"] = int(data["likelihood"]) * int(data["impact"])
    if key == "budget":
        if data.get("planned") == "" and data.get("quantity") != "" and data.get("unitCost") != "":
            data["planned"] = round(data["quantity"] * data["unitCost"], 2)
        p, a = data.get("planned"), data.get("actual")
        data["variance"] = "" if p == "" and a == "" else round((p or 0) - (a or 0), 2)
    if key == "communications" and data.get("status") == "Sent" and not data.get("sentDate"):
        data["sentDate"] = today()
    return data


def next_ref(mod: dict, existing_refs: list[str]) -> str:
    if not mod.get("refPrefix"):
        return ""
    n = max([int(re.sub(r"\D", "", r) or 0) for r in existing_refs if r] or [0])
    return f"{mod['refPrefix']}{n + 1:03d}"


def template_records(mod: dict, start: str) -> list[dict]:
    """The section's standard records: the phase plan (from `start`) or the closure checklist."""
    t = mod.get("template")
    if not t:
        return []
    if t.get("kind") != "phasePlan":
        return [dict(r) for r in t.get("records", [])]
    plan = [("Discovery", 28), ("Pilot", 42), ("Waves", 84), ("Hypercare", 28), ("Close", 14),
            ("Post-Migration Delivery & Technical Implementation", 42)]
    recs, s = [], start
    for phase, days in plan:
        e = add_days(s, days - 1)
        recs.append({"title": f"{phase} phase", "phase": phase, "type": "Phase", "startDate": s, "endDate": e})
        s = add_days(e, 1)
    end = {r["phase"]: r["endDate"] for r in recs}
    begin = {r["phase"]: r["startDate"] for r in recs}
    last = PHASES[-1]
    for title, phase, when in [
        ("Discovery complete: design and scope signed off", "Discovery", end["Discovery"]),
        ("Pilot go / no-go", "Pilot", end["Pilot"]),
        ("Wave 1 cutover", "Waves", add_days(begin["Waves"], 14)),
        ("Final wave complete", "Waves", end["Waves"]),
        ("Hypercare exit criteria met", "Hypercare", end["Hypercare"]),
        ("Project closure and sign-off", "Close", end["Close"]),
        ("Post-migration technical implementation complete", last, end[last]),
    ]:
        recs.append({"title": title, "phase": phase, "type": "Milestone", "endDate": when})
    return [{"status": "Not Started", **r} for r in recs]


# ---------- Dashboard ----------

def current_phase(milestones: list[dict], reports: list[dict], t: str) -> str:
    bars = sorted((m for m in milestones if m.get("type") == "Phase" and m.get("startDate")
                   and m["startDate"] <= t <= (m.get("endDate") or "")),
                  key=lambda m: PHASES.index(m["phase"]) if m.get("phase") in PHASES else 99)
    if bars:
        return bars[0]["phase"]
    latest = max(reports, key=lambda r: r.get("periodEnding") or "", default=None)
    return (latest or {}).get("phase") or ""


def summarize(items: list[dict], t: str, project: dict) -> dict:
    """Counts for the dashboard. items: [{module, data, ...}]."""
    def by(k):
        return [i["data"] for i in items if i["module"] == k]

    def count(rows, pred):
        return sum(1 for r in rows if pred(r))

    def total(rows, k):
        return round(sum(float(r.get(k) or 0) for r in rows), 2)

    reminders = by("reminders")
    open_rem = [r for r in reminders if r.get("status") != "Completed"]
    risks = by("risks")
    open_risks = [r for r in risks if r.get("status") != "Closed"]
    issues = by("issues")
    open_issues = [i for i in issues if i.get("status") in ("Open", "In Progress")]
    actions = by("actions")
    open_actions = [a for a in actions if a.get("status") not in DONE_ACTION]
    milestones = by("milestones")
    reports = by("statusReports")
    latest = max(reports, key=lambda r: r.get("periodEnding") or "", default=None)
    assumptions = by("assumptions")
    deps = by("dependencies")
    open_deps = [d for d in deps if d.get("status") not in ("Met", "Missed")]
    costs = by("budget")
    comms = by("communications")
    pending = [c for c in comms if c.get("status") in ("Pending", "Scheduled")]
    closure = by("closure")
    upcoming = sorted((m for m in milestones if m.get("status") != "Complete" and (m.get("endDate") or "") >= t),
                      key=lambda m: m["endDate"])
    changes, decisions, documents = by("changes"), by("decisions"), by("documents")
    return {
        "status": {"overallRag": (latest or {}).get("overallRag", ""), "periodEnding": (latest or {}).get("periodEnding", ""),
                   "reports": len(reports), "phase": current_phase(milestones, reports, t)},
        "reminders": {"total": len(reminders), "open": len(open_rem),
                      "overdue": count(open_rem, lambda r: r.get("dueDate") and r["dueDate"] < t),
                      "dueToday": count(open_rem, lambda r: r.get("dueDate") == t)},
        "actions": {"total": len(actions), "open": len(open_actions),
                    "overdue": count(open_actions, lambda a: a.get("dueDate") and a["dueDate"] < t),
                    "blocked": count(open_actions, lambda a: a.get("status") == "Blocked"),
                    "completed": count(actions, lambda a: a.get("status") == "Completed")},
        "milestones": {"total": len(milestones), "complete": count(milestones, lambda m: m.get("status") == "Complete"),
                       "late": count(milestones, lambda m: m.get("status") != "Complete" and (m.get("endDate") or "9") < t),
                       "atRisk": count(milestones, lambda m: m.get("status") in ("At Risk", "Delayed")),
                       "dueSoon": count(upcoming, lambda m: m["endDate"] <= add_days(t, 30)),
                       "next": ({k: upcoming[0].get(k) for k in ("title", "endDate", "phase")} if upcoming else None)},
        "risks": {"total": len(risks), "open": len(open_risks),
                  "critical": count(open_risks, lambda r: (r.get("score") or 0) >= 20),
                  "high": count(open_risks, lambda r: 12 <= (r.get("score") or 0) < 20)},
        "issues": {"total": len(issues), "open": len(open_issues),
                   "criticalOpen": count(open_issues, lambda i: i.get("priority") == "Critical")},
        "assumptions": {"total": len(assumptions), "open": count(assumptions, lambda a: a.get("status") == "Open"),
                        "overdue": count(assumptions, lambda a: a.get("status") == "Open" and a.get("validateBy")
                                         and a["validateBy"] < t),
                        "invalid": count(assumptions, lambda a: a.get("status") == "Invalid")},
        "dependencies": {"total": len(deps), "open": len(open_deps),
                         "atRisk": count(open_deps, lambda d: d.get("status") == "At Risk"
                                         or (d.get("requiredBy") and d["requiredBy"] < t))},
        "changes": {"total": len(changes), "pending": count(changes, lambda c: c.get("status") in ("Requested", "Under Review"))},
        "decisions": {"total": len(decisions), "proposed": count(decisions, lambda d: d.get("status") == "Proposed")},
        "budget": {"lines": len(costs), "approved": float(project.get("approvedBudget") or 0),
                   "planned": total(costs, "planned"), "actual": total(costs, "actual"),
                   "currency": project.get("currency") or "GBP"},
        "communications": {"total": len(comms), "pending": len(pending),
                           "overdue": count(pending, lambda c: c.get("sendDate") and c["sendDate"] < t)},
        "closure": {"total": len(closure), "done": count(closure, lambda c: c.get("status") in ("Done", "Not Applicable"))},
        "stakeholders": {"total": len(by("stakeholders"))},
        "meetings": {"total": len(by("meetings"))},
        "documents": {"total": len(documents)},
        "lessons": {"total": len(by("lessons"))},
    }


def status_draft(items: list[dict], t: str, project: dict) -> dict:
    """A new status report filled in from the project as it stands. RAGs: Red for critical risks or
    issues, late milestones or overspend; Amber for early warning signs."""
    s = summarize(items, t, project)
    week_ago, fortnight = add_days(t, -7), add_days(t, 14)

    def by(k):
        return [i for i in items if i["module"] == k]

    def label(i, text):
        return f"{i['ref'] + ' ' if i.get('ref') else ''}{text}"

    def first_line(x):
        return str(x or "").split("\n")[0]

    def lines(xs):
        return "\n".join(f"• {x}" for x in xs)

    achieved = (
        [f"Milestone reached: {label(m, m['data'].get('title'))}" for m in by("milestones")
         if m["data"].get("status") == "Complete" and (m.get("updatedAt") or "")[:10] >= week_ago]
        + [f"Completed {label(a, first_line(a['data'].get('description')))}" for a in by("actions")
           if a["data"].get("status") == "Completed" and (a.get("updatedAt") or "")[:10] >= week_ago]
        + [f"Sent “{c['data'].get('subject')}” to {c['data'].get('audience')}" for c in by("communications")
           if c["data"].get("status") == "Sent" and (c["data"].get("sentDate") or "") >= week_ago])
    nxt = (
        [f"{m['data'].get('title')} (due {m['data']['endDate']})" for m in by("milestones")
         if m["data"].get("status") != "Complete" and t <= (m["data"].get("endDate") or "") <= fortnight]
        + [f"{label(a, first_line(a['data'].get('description')))}"
           f"{' — ' + a['data']['owner'] if a['data'].get('owner') else ''} (due {a['data']['dueDate']})"
           for a in by("actions") if a["data"].get("status") not in DONE_ACTION and a["data"].get("dueDate")
           and a["data"]["dueDate"] <= fortnight])
    bad_issues = [i for i in by("issues") if i["data"].get("status") in ("Open", "In Progress")
                  and i["data"].get("priority") in ("High", "Critical")]
    risky_deps = [d for d in by("dependencies") if d["data"].get("status") == "At Risk"
                  or (d["data"].get("status") not in ("Met", "Missed") and d["data"].get("requiredBy")
                      and d["data"]["requiredBy"] < t)]
    key_risks = (
        [f"Risk {label(r, r['data'].get('title'))} ({risk_rating(r['data'].get('score'))})" for r in by("risks")
         if r["data"].get("status") != "Closed" and (r["data"].get("score") or 0) >= 12]
        + [f"Issue {label(i, i['data'].get('title'))} ({i['data'].get('priority')})" for i in bad_issues]
        + [f"Dependency {label(d, d['data'].get('title'))} ({d['data'].get('status')})" for d in risky_deps])
    needed = [f"Approve or reject {label(c, c['data'].get('title'))}" for c in by("changes")
              if c["data"].get("status") in ("Requested", "Under Review")]

    b = s["budget"]
    overspend = b["actual"] > b["approved"] if b["approved"] else (b["planned"] > 0 and b["actual"] > b["planned"])
    near = b["approved"] > 0 and max(b["planned"], b["actual"]) > b["approved"] * 0.9
    schedule = "Red" if s["milestones"]["late"] else "Amber" if s["milestones"]["atRisk"] or s["actions"]["overdue"] else "Green"
    budget = "Red" if overspend else "Amber" if near else "Green"
    scope = "Amber" if any(c["data"].get("type") == "Scope" and c["data"].get("status") in ("Requested", "Under Review")
                           for c in by("changes")) else "Green"
    risks = ("Red" if s["risks"]["critical"] or s["issues"]["criticalOpen"] else
             "Amber" if s["risks"]["high"] or bad_issues or risky_deps else "Green")
    overall = max((schedule, budget, scope, risks), key=RAG.index)
    return {"periodEnding": t, "phase": s["status"]["phase"], "overallRag": overall, "scheduleRag": schedule,
            "budgetRag": budget, "scopeRag": scope, "risksRag": risks, "summary": "",
            "achievements": lines(achieved), "nextSteps": lines(nxt), "keyRisks": lines(key_risks),
            "decisionsNeeded": lines(needed)}
