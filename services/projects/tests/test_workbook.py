"""Projects as Excel workbooks: export, a blank template, and importing into a project or as a new one."""
import io

from openpyxl import load_workbook

from app import workbook
from tests.test_projects import AMY, BOB, H, c, new_project, url  # noqa: F401  (c is the fixture)

XLSX = {"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def fill(c, pid):
    def add(key, body):
        r = c.post(url(pid, f"/items/{key}"), headers=AMY, json=body)
        assert r.status_code == 200, r.text
        return r.json()
    add("risks", {"title": "COO number cut off", "description": "Line ends", "likelihood": "5", "impact": "4", "owner": "Amy",
                  "updates": [{"date": "2026-11-20", "user": "Amy", "notes": "Raised with the carrier\nwaiting to hear"}]})
    add("decisions", {"title": "Use Intune", "description": "For every phone", "madeBy": ["Amy", "Bob"], "dateMade": "2026-11-01"})
    add("meetings", {"title": "Kick-off", "date": "2026-11-02", "attendees": "Amy, Bob",
                     "actions": [{"description": "Order SIMs", "assignedTo": "Bob", "dueDate": "2026-11-10", "status": "Open"}]})
    add("actions", {"description": "Write the comms", "priority": "High"})
    add("budget", {"category": "Hardware", "description": "Phones", "quantity": "10", "unitCost": "250"})


def test_export_and_import_as_a_new_project(c):
    pid = new_project(c)
    c.patch(url(pid), headers=AMY, json={"approvedBudget": "5000"})
    fill(c, pid)
    r = c.get(url(pid, "/export.xlsx"), headers=AMY)
    assert r.status_code == 200 and "Laptop%20refresh" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames[:3] == ["Read me", "Project", "Status Reports"]
    risks = list(wb["Risk Register"].values)
    assert risks[0][0] == "Ref" and risks[0][1] == "Title *"
    assert risks[1][0] == "RSK-001" and "Critical (20)" in risks[1]
    assert [r[1] for r in list(wb["Action Log"].values)[1:]] == ["Write the comms"]   # the meeting's action isn't repeated

    preview = c.post("/projects/api/projects/import?preview=1", headers={**BOB, **XLSX}, content=r.content).json()
    assert preview["saved"] is False and preview["name"] == "Laptop refresh" and not preview["problems"]
    assert {s["key"]: s["count"] for s in preview["sections"]} == {"risks": 1, "actions": 1, "decisions": 1, "meetings": 1, "budget": 1}
    assert c.get("/projects/api/projects", headers=BOB).json()["projects"] == []   # a preview saves nothing

    made = c.post("/projects/api/projects/import", headers={**BOB, **XLSX}, content=r.content).json()
    new = made["id"]
    p = c.get(url(new), headers=BOB).json()
    assert p["role"] == "Owner" and p["approvedBudget"] == 5000 and p["description"] == "250 laptops"
    risk = c.get(url(new, "/items/risks"), headers=BOB).json()["items"][0]
    assert risk["ref"] == "RSK-001" and risk["data"]["score"] == 20 and risk["data"]["owner"] == "Amy"
    assert risk["data"]["updates"][0]["notes"] == "Raised with the carrier\nwaiting to hear"
    assert c.get(url(new, "/items/decisions"), headers=BOB).json()["items"][0]["data"]["madeBy"] == ["Amy", "Bob"]
    actions = c.get(url(new, "/items/actions"), headers=BOB).json()["items"]
    assert sorted(a["data"]["description"] for a in actions) == ["Order SIMs", "Write the comms"]   # the meeting made its own
    assert c.get(url(new, "/items/budget"), headers=BOB).json()["items"][0]["data"]["planned"] == 2500


def test_import_into_a_project_adds_and_checks_every_row(c):
    src = new_project(c, name="Source")
    fill(c, src)
    data = c.get(url(src, "/export.xlsx"), headers=AMY).content
    pid = new_project(c, name="Target")
    c.post(url(pid, "/items/risks"), headers=AMY, json={"title": "Old", "description": "d", "likelihood": "1", "impact": "1"})
    r = c.post(url(pid, "/import"), headers={**AMY, **XLSX}, content=data).json()
    assert r["saved"] and r["total"] == 5
    refs = [i["ref"] for i in c.get(url(pid, "/items/risks"), headers=AMY).json()["items"]]
    assert refs == ["RSK-001", "RSK-002"]   # added after what was there
    assert c.get(url(pid), headers=AMY).json()["description"] == "250 laptops"   # not overwritten

    # a hand-made workbook: loose headers, UK dates, a bad row: nothing is added
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "issues log"
    ws.append(["title", "Priority", "Due date", "Something else"])
    ws.append(["Phones late", "high", "05/12/2026", "x"])
    ws.append(["", "High", "", ""])   # no title
    wb.create_sheet("Notes")
    buf = io.BytesIO()
    wb.save(buf)
    r = c.post(url(pid, "/import"), headers={**AMY, **XLSX}, content=buf.getvalue()).json()
    assert r["saved"] is False and r["problems"] == ["Issues Log, row 3: Title is required."] and r["skipped"] == ["Notes"]
    assert c.get(url(pid, "/items/issues"), headers=AMY).json()["items"] == []
    ws.delete_rows(3)
    buf = io.BytesIO()
    wb.save(buf)
    assert c.post(url(pid, "/import"), headers={**AMY, **XLSX}, content=buf.getvalue()).json()["saved"]
    issue = c.get(url(pid, "/items/issues"), headers=AMY).json()["items"][0]["data"]
    assert issue["priority"] == "High" and issue["dueDate"] == "2026-12-05"


def test_import_rules(c):
    pid = new_project(c)
    c.post(url(pid, "/members"), headers=AMY, json={"email": "bob@nscale.com", "role": "Viewer"})
    blank = c.get("/projects/api/template.xlsx", headers=BOB)
    assert blank.status_code == 200 and load_workbook(io.BytesIO(blank.content))["Project"]["A2"].value == "Name *"
    assert c.post(url(pid, "/import"), headers={**BOB, **XLSX}, content=blank.content).status_code == 403   # viewers can't
    assert c.get(url(pid, "/export.xlsx"), headers=BOB).status_code == 200   # but can export
    assert c.post(url(pid, "/import"), headers={**AMY, **XLSX}, content=b"not a workbook").status_code == 422
    assert c.post(url(pid, "/import"), headers={**AMY, **XLSX}, content=blank.content).status_code == 422   # nothing in it
    r = c.post("/projects/api/projects/import", headers={**AMY, **XLSX}, content=blank.content).json()
    assert r["saved"] is False and r["problems"] == ["Project sheet: give the project a name."]
    assert c.post("/projects/api/projects/import", headers={**XLSX, "x-cp-user": AMY["x-cp-user"]}, content=blank.content).status_code == 403
    assert workbook.sheet_name({"label": "A/B: c"}) == "A-B- c"
