"""Documents: uploading files to the Document Library, versions, and editing Word, Excel and CSV files."""
import io
from urllib.parse import quote

import docx
import openpyxl

from tests.test_projects import AMY, BOB, c, new_project, url  # noqa: F401  (c is the fixture)


def doc_record(c, pid, title="Business case"):
    r = c.post(url(pid, "/items/documents"), headers=AMY, json={"title": title, "type": "Design", "status": "Draft"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def upload(c, pid, iid, name, data, headers=AMY, **extra):
    return c.post(url(pid, f"/items/documents/{iid}/file"), headers={**headers, "X-Filename": quote(name), **extra}, content=data)


def word(*paras):
    d = docx.Document()
    d.add_heading("Intune migration", 1)
    for p in paras:
        d.add_paragraph(p)
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "Item", "Cost", "Laptops", "250"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_upload_download_versions(c):
    pid = new_project(c)
    iid = doc_record(c, pid)
    # A link isn't needed any more, and the file can't be set from the form.
    r = c.put(url(pid, f"/items/documents/{iid}"), headers=AMY, json={"title": "Business case", "file": {"name": "evil.exe"}})
    assert r.status_code == 200 and r.json()["data"]["file"] is None

    r = upload(c, pid, iid, "costs.csv", b"Item,Cost\nLaptops,250\n")
    assert r.status_code == 200, r.text
    f = r.json()["data"]["file"]
    assert (f["name"], f["kind"], f["version"], f["size"]) == ("costs.csv", "text", 1, 22)
    got = c.get(url(pid, f"/items/documents/{iid}/file"), headers=AMY)
    assert got.content == b"Item,Cost\nLaptops,250\n" and got.headers["content-disposition"].startswith("attachment")
    assert got.headers["x-content-type-options"] == "nosniff"

    # An edit from the page says which version it started from; a stale one is refused.
    assert upload(c, pid, iid, "costs.csv", b"Item,Cost\nLaptops,300\n", **{"X-Base-Version": "1"}).json()["data"]["file"]["version"] == 2
    assert upload(c, pid, iid, "costs.csv", b"x", **{"X-Base-Version": "1"}).status_code == 409
    vs = c.get(url(pid, f"/items/documents/{iid}/file/versions"), headers=AMY).json()["versions"]
    assert [v["version"] for v in vs] == [2, 1]
    assert c.get(url(pid, f"/items/documents/{iid}/file?v=1"), headers=AMY).content.endswith(b"250\n")
    r = c.post(url(pid, f"/items/documents/{iid}/file/restore"), headers=AMY, json={"version": 1})
    assert r.json()["data"]["file"]["version"] == 3
    assert c.get(url(pid, f"/items/documents/{iid}/file"), headers=AMY).content.endswith(b"250\n")

    # Only pictures, PDFs and plain text show in the page; HTML always downloads.
    upload(c, pid, iid, "page.html", b"<script>alert(1)</script>")
    got = c.get(url(pid, f"/items/documents/{iid}/file?view=1"), headers=AMY)
    assert got.headers["content-disposition"].startswith("attachment")
    upload(c, pid, iid, "shot.png", b"\x89PNG....")
    got = c.get(url(pid, f"/items/documents/{iid}/file?view=1"), headers=AMY)
    assert got.headers["content-disposition"].startswith("inline") and got.headers["content-type"] == "image/png"
    assert "sandbox" in got.headers["content-security-policy"]

    # Checks: empty, not really a Word file, names cleaned.
    assert upload(c, pid, iid, "a.txt", b"").status_code == 422
    assert upload(c, pid, iid, "fake.docx", b"not a zip").status_code == 422
    assert upload(c, pid, iid, "../../etc/passwd", b"x").json()["data"]["file"]["name"] == "_.._etc_passwd"


def test_who_can_upload_and_files_go_with_the_record(c):
    pid = new_project(c)
    iid = doc_record(c, pid)
    upload(c, pid, iid, "a.txt", b"hello")
    assert c.post(url(pid, "/members"), headers=AMY, json={"email": "bob@nscale.com", "role": "Viewer"}).status_code == 200
    assert c.get(url(pid, f"/items/documents/{iid}/file"), headers=BOB).content == b"hello"
    assert upload(c, pid, iid, "b.txt", b"x", headers=BOB).status_code == 403
    # Other sections have no files.
    risk = c.post(url(pid, "/items/risks"), headers=AMY, json={"title": "Supply", "description": "Late", "likelihood": "3", "impact": "3"}).json()
    assert c.post(url(pid, f"/items/risks/{risk['id']}/file"), headers={**AMY, "X-Filename": "a.txt"}, content=b"x").status_code == 404
    # Deleting the record deletes its files.
    assert c.delete(url(pid, f"/items/documents/{iid}"), headers=AMY).status_code == 200
    assert main_store(c).files(iid) == []


def main_store(c):
    from app import main
    return main.store


def test_edit_word(c):
    pid = new_project(c)
    iid = doc_record(c, pid)
    upload(c, pid, iid, "case.docx", word("First paragraph.", "Second paragraph."))
    d = c.get(url(pid, f"/items/documents/{iid}/file/docx"), headers=AMY).json()
    kinds = [(b["kind"], b.get("style")) for b in d["blocks"]]
    assert kinds == [("p", "Heading 1"), ("p", "Normal"), ("p", "Normal"), ("table", None)]
    assert "Heading 2" in d["styles"]
    blocks = d["blocks"]
    blocks[1]["runs"] = [{"t": "Changed ", "b": False}, {"t": "bold", "b": True}]
    del blocks[2]                                                     # removed
    blocks.insert(2, {"kind": "p", "style": "Heading 2", "runs": [{"t": "New section"}], "like": 1})
    blocks[3]["rows"] = [["Item", "Cost"], ["Laptops", "300"], ["Docks", "40"]]   # changed and added a row
    r = c.put(url(pid, f"/items/documents/{iid}/file/docx"), headers=AMY, json={"version": d["version"], "blocks": blocks})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["file"]["version"] == 2
    out = docx.Document(io.BytesIO(c.get(url(pid, f"/items/documents/{iid}/file"), headers=AMY).content))
    assert [p.text for p in out.paragraphs] == ["Intune migration", "Changed bold", "New section"]
    assert out.paragraphs[1].runs[1].bold and out.paragraphs[2].style.name == "Heading 2"
    assert [[c.text for c in r.cells] for r in out.tables[0].rows] == [["Item", "Cost"], ["Laptops", "300"], ["Docks", "40"]]
    # Saving from an older version is refused.
    assert c.put(url(pid, f"/items/documents/{iid}/file/docx"), headers=AMY, json={"version": 1, "blocks": blocks}).status_code == 409


def test_edit_excel(c):
    pid = new_project(c)
    iid = doc_record(c, pid)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Costs"
    ws.append(["Item", "Cost"])
    ws.append(["Laptops", 250])
    ws["B3"] = "=SUM(B2:B2)"
    ws["A1"].font = openpyxl.styles.Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    upload(c, pid, iid, "costs.xlsx", buf.getvalue())
    d = c.get(url(pid, f"/items/documents/{iid}/file/xlsx"), headers=AMY).json()
    assert d["sheets"][0]["name"] == "Costs" and d["sheets"][0]["rows"] == [["Item", "Cost"], ["Laptops", "250"], ["", "=SUM(B2:B2)"]]
    assert d["readOnly"] is False
    edits = [{"sheet": "Costs", "r": 1, "c": 1, "v": "300"}, {"sheet": "Costs", "r": 3, "c": 0, "v": "2026-12-01"},
             {"sheet": "Costs", "r": 2, "c": 1, "v": "=SUM(B2:B3)"}, {"sheet": "Nope", "r": 0, "c": 0, "v": "x"}]
    r = c.put(url(pid, f"/items/documents/{iid}/file/xlsx"), headers=AMY, json={"version": d["version"], "edits": edits})
    assert r.status_code == 200, r.text
    out = openpyxl.load_workbook(io.BytesIO(c.get(url(pid, f"/items/documents/{iid}/file"), headers=AMY).content))["Costs"]
    assert out["B2"].value == 300 and out["B3"].value == "=SUM(B2:B3)" and out["A1"].font.bold
    assert out["A4"].value.year == 2026 and out["A4"].is_date
