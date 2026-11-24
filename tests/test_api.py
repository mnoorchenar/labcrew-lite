"""The API end to end: start a session, wait for the human step, approve, then reject; plot and audit endpoints."""
import time

from fastapi.testclient import TestClient

from gwascrew.api import app
from tests.conftest import wide_shortlist

client = TestClient(app)


def wait_for(sid, statuses, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = client.get(f"/api/sessions/{sid}").json()
        if s["status"] in statuses:
            return s
        time.sleep(0.3)
    raise TimeoutError(s)


def test_full_session_over_http(monkeypatch):
    wide_shortlist(monkeypatch)
    assert client.get("/").status_code == 200
    sid = client.post("/api/sessions", json={"question": "Which variants are associated?", "reviewer": "tester"}).json()["session_id"]
    s = wait_for(sid, {"awaiting_approval", "needs_review", "error"})
    assert s["status"] == "awaiting_approval", s
    proposals = s["rounds"][0]["proposals"]

    assert client.post(f"/api/sessions/{sid}/decision", json={"approve": ["var0333"]}).status_code == 422  # not proposed
    plot = client.get(f"/api/sessions/{sid}/plot").json()
    assert len(plot["neglog10p"]) == len(plot["variant_ids"]) and plot["engine"]["coloc"]

    assert client.post(f"/api/sessions/{sid}/decision", json={"approve": proposals[:2], "comment": "top two"}).status_code == 200
    s = wait_for(sid, {"awaiting_approval", "finished", "needs_review", "error"})
    assert s["status"] == "awaiting_approval" and len(s["rounds"]) == 2, s
    assert client.post(f"/api/sessions/{sid}/decision", json={"approve": []}).status_code == 200   # reject all
    s = wait_for(sid, {"finished", "needs_review", "error"})
    assert s["status"] == "finished" and s["truth"]["power"] == 0.4

    audit = client.get(f"/api/sessions/{sid}/audit").json()
    assert [(a["approved"], a["comment"]) for a in audit] == [(proposals[:2], "top two"), ([], "")]
    tools = [t["tool"] for t in client.get(f"/api/sessions/{sid}/tools").json()]
    assert tools.count("run_assay") == 1 and "truth_check" in tools
    assert client.post(f"/api/sessions/{sid}/decision", json={"approve": []}).status_code == 409   # nothing waiting


def test_unknown_session():
    assert client.get("/api/sessions/nope").status_code == 404


def test_out_of_scope_question_is_rejected():
    r = client.post("/api/sessions", json={"question": "What is the weather in Paris today?"})
    assert r.status_code == 422 and "out of scope" in r.text


def test_example_questions_are_in_scope():
    from gwascrew.api import IN_SCOPE
    for q in client.get("/api/examples").json():
        assert IN_SCOPE.search(q["question"]), q


def test_full_report_html_and_pdf(monkeypatch):
    """The Full report tab: one self-contained page with every round's hash, and the same report as a PDF."""
    import pytest
    from gwascrew import report
    sid = client.post("/api/sessions", json={"question": "Which variants are associated?", "reviewer": "tester"}).json()["session_id"]
    assert client.get(f"/api/sessions/{sid}/report.html").status_code in (200, 409)   # 409 until a round has a report
    s = wait_for(sid, {"awaiting_approval", "needs_review", "error"})
    assert s["status"] == "awaiting_approval", s
    assert client.post(f"/api/sessions/{sid}/decision", json={"approve": s["rounds"][0]["proposals"]}).status_code == 200
    s = wait_for(sid, {"awaiting_approval", "finished", "needs_review", "error"})

    r = client.get(f"/api/sessions/{sid}/report.html?download=1")
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
    page = r.text
    assert "SIMULATED" in page and "Appendix B" in page and "<script" not in page
    for rd in s["rounds"]:
        assert rd["report_sha256"] in page
    assert client.get(f"/api/sessions/{sid}/report.html").headers["content-disposition"].startswith("inline")
    assert client.get("/api/sessions/nope/report.html").status_code == 404

    if not report.pdf_available():
        pytest.skip("pandoc / xelatex not installed")
    pdf = client.get(f"/api/sessions/{sid}/report.pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"


def test_report_text_never_becomes_markup():
    """LLM text inside a round report is shown as text: no raw HTML in the page, no raw LaTeX in the PDF source."""
    from gwascrew import report
    html_out = report.md_block("x <script>alert(1)</script> <img src=x onerror=alert(1)>")
    assert "<script" not in html_out and "<img" not in html_out
    defused = report.defuse_raw("```{=latex}\n\\input{x}\n``` and `\\input{y}`{=latex}")
    assert "```{=" not in defused and "`{=" not in defused     # pandoc then prints the block as text


def test_how_it_works_guide_is_served():
    page = client.get("/").text
    assert 'data-p="guide"' in page and "/static/guide.html" in page
    guide = client.get("/static/guide.html")
    assert guide.status_code == 200 and "How the agents connect" in guide.text
    pdf = client.get("/static/guide.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
