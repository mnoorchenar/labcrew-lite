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
