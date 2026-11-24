"""Purpose: the FastAPI service. It starts analyses, reports their state, takes the human decision,
and serves the review page (static/index.html).

    POST /api/sessions                 start an analysis (the CrewAI Flow runs in a background thread)
    GET  /api/sessions/{id}            status, per-round report, proposals, checks, assay, truth check
    GET  /api/sessions/{id}/plot       Manhattan + QQ data (computed by the R engine)
    POST /api/sessions/{id}/decision   the human's approve / reject: unblocks the waiting Flow
    GET  /api/sessions/{id}/audit      human decisions        GET /api/sessions/{id}/tools   every tool call
    GET  /api/sessions/{id}/report.html  the full report as one self-contained page (?download=1 to save it)
    GET  /api/sessions/{id}/report.pdf   the same report as a PDF (pandoc + XeLaTeX on the server)

How the human step works: the Flow's @human_feedback step asks a QueueProvider for an answer, and that call
blocks on a queue until POST /decision puts the reviewer's reply on it. No polling inside the Flow.
Run: uvicorn gwascrew.api:app --port 8770, then open http://localhost:8770
"""
from __future__ import annotations

import os
import queue
import re
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from gwascrew import evidence, report
from gwascrew.crew import load_env, model_chain
from gwascrew.flow import SESSIONS, GwasFlow

STATIC = Path(__file__).resolve().parents[1] / "static"
RUNS: dict[str, dict] = {}   # session id -> {"flow", "answers" queue, "error"}


DECISION_TIMEOUT_S = 3600   # a reviewer who never answers must not hold a thread forever
SHUTDOWN_REPLY = "reject all; the server shut down before a decision"
SHUTTING_DOWN = threading.Event()


class QueueProvider:
    def __init__(self):
        self.answers: queue.Queue[str] = queue.Queue()

    def request_feedback(self, context, flow):
        if SHUTTING_DOWN.is_set():
            return SHUTDOWN_REPLY
        try:
            return self.answers.get(timeout=DECISION_TIMEOUT_S)   # blocks the Flow thread until the reviewer decides
        except queue.Empty:
            return "reject all; no decision within the time limit"   # fail closed: never an approval


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # Ctrl+C: release every Flow still waiting for a reviewer (fail closed: a reject, never an approval).
    # Otherwise their worker threads block interpreter exit for up to DECISION_TIMEOUT_S, and a second Ctrl+C
    # tears Python down under them, which prints "KeyError: 'litellm'" tracebacks.
    SHUTTING_DOWN.set()
    for run in RUNS.values():
        run["provider"].answers.put(SHUTDOWN_REPLY)


app = FastAPI(title="GWAS Crew", description="CrewAI agents + R statistics + human approval. SIMULATED data only.",
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")   # favicon, guided tour


# Scope guard: this app only answers GWAS / QC / assay questions. A plain keyword gate, deterministic and testable.
IN_SCOPE = re.compile(r"variant|gwas|snp|genotyp|allele|associat|trait|assay|\bqc\b|sop|guideline|lambda|inflation|"
                      r"bonferroni|threshold|significan|\bhits?\b|call.?rate|hardy|stratif|posterior|pip\b|"
                      r"annotation|replicat|power|filter|var\d{4}|study|studies|p-?value|manhattan", re.I)


class StartRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)

    @field_validator("question")
    @classmethod
    def in_scope(cls, v: str) -> str:
        if not IN_SCOPE.search(v):
            raise ValueError("out of scope: ask about GWAS variants, QC, thresholds, the assay or the SOPs")
        return v
    reviewer: str = Field(default="scientist", max_length=60)
    rounds: int = Field(default=2, ge=1, le=3)
    seed: int = 2026


class DecisionRequest(BaseModel):
    approve: list[str] = Field(default_factory=list, description="variant ids to send to the assay; empty = reject all")
    comment: str = Field(default="", max_length=500)


def _run(session_id: str) -> dict:
    if session_id not in RUNS:
        raise HTTPException(404, "unknown session")
    return RUNS[session_id]


@app.get("/")
def page():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    load_env()
    mock = os.environ.get("MOCK_LLM") == "1"
    return {"ok": True, "data": "SIMULATED / FICTIONAL",
            "llm": {"mode": "mock" if mock else "live", "models": [] if mock else model_chain()},
            "pdf": report.pdf_available()}


@app.post("/api/sessions")
def start(req: StartRequest):
    sid = uuid.uuid4().hex[:12]
    provider = QueueProvider()
    flow = GwasFlow(feedback_provider=provider, suppress_flow_events=True, tracing=False)
    RUNS[sid] = {"flow": flow, "provider": provider, "error": None}

    def work():
        try:
            flow.kickoff(inputs={"question": req.question, "seed": req.seed, "rounds_max": req.rounds,
                                 "reviewer": req.reviewer, "session_id": sid})
        except Exception as exc:  # noqa: BLE001 - shown to the user, never swallowed
            RUNS[sid]["error"] = f"{type(exc).__name__}: {exc}"

    threading.Thread(target=work, daemon=True).start()
    return {"session_id": sid}


@app.get("/api/sessions/{session_id}")
def state(session_id: str):
    run = _run(session_id)
    st = run["flow"].state
    s = SESSIONS.get(session_id)
    calls = evidence.tool_calls(session_id) if s else []
    return {"session_id": session_id, "status": "error" if run["error"] else st.status,
            "error": run["error"], "stop_reason": st.stop_reason, "question": st.question,
            "round": len(st.rounds), "rounds": st.rounds, "truth": st.truth, "rounds_max": st.rounds_max,
            "current_round": s.round_no if s else 1,
            # tools called so far in the current round, in order: drives the live progress stepper
            "progress": [c["tool"] for c in calls if c["inputs"].get("round") == (s.round_no if s else 1)]}


@app.get("/api/examples")
def examples():
    """Example questions for the sidebar chips: the labelled evaluation questions."""
    import yaml
    qs = yaml.safe_load((STATIC.parent / "eval" / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    return [{"id": q["id"], "question": q["question"]} for q in qs]


@app.get("/api/sessions/{session_id}/plot")
def plot(session_id: str):
    _run(session_id)
    s = SESSIONS.get(session_id)
    if s is None or s.result is None:
        raise HTTPException(409, "the analysis has not run yet")
    return {**s.result.plot, "bonferroni_threshold": s.result.bonferroni_threshold, "engine": s.engine}


@app.post("/api/sessions/{session_id}/decision")
def decision(session_id: str, req: DecisionRequest):
    run = _run(session_id)
    st = run["flow"].state
    if st.status != "awaiting_approval":
        raise HTTPException(409, f"nothing to approve (status: {st.status})")
    proposals = st.rounds[-1]["proposals"]
    if bad := [v for v in req.approve if v not in proposals]:
        raise HTTPException(422, f"not proposed this round: {bad}")
    reply = f"approve {','.join(req.approve)}" if req.approve else "reject all"
    st.status = "running"   # the Flow continues; the next state update comes from the Flow itself
    run["provider"].answers.put(reply + (f"; {req.comment}" if req.comment else ""))
    return {"accepted": reply}


@app.get("/api/sessions/{session_id}/audit")
def audit(session_id: str):
    _run(session_id)
    return evidence.audit_log(session_id)


@app.get("/api/sessions/{session_id}/tools")
def tools(session_id: str):
    _run(session_id)
    return [{k: r[k] for k in ("timestamp", "tool", "tool_version", "inputs", "status")}
            for r in evidence.tool_calls(session_id)]


def _report_parts(session_id: str) -> dict:
    run = _run(session_id)
    st = run["flow"].state
    if not any(r.get("report_md") for r in st.rounds):
        raise HTTPException(409, "no round has produced a report yet")
    return report.build_parts(st, SESSIONS.get(session_id), evidence.audit_log(session_id),
                              evidence.tool_calls(session_id), status="error" if run["error"] else st.status)


def _attachment(name: str, download: bool) -> dict:
    return {"Content-Disposition": f'{"attachment" if download else "inline"}; filename="{name}"'}


@app.get("/api/sessions/{session_id}/report.html")
def report_html(session_id: str, download: bool = False):
    page = report.render_html(_report_parts(session_id))
    return HTMLResponse(page, headers=_attachment(f"labcrew_lite_report_{session_id}.html", download))


@app.get("/api/sessions/{session_id}/report.pdf")
def report_pdf(session_id: str, download: bool = True):
    parts = _report_parts(session_id)
    if not report.pdf_available():
        raise HTTPException(503, "PDF export needs pandoc and xelatex (LaTeX) on the server")
    try:
        pdf = report.render_pdf(parts)
    except Exception as exc:  # noqa: BLE001 - shown to the user
        raise HTTPException(500, f"PDF build failed: {exc}") from exc
    return Response(pdf, media_type="application/pdf",
                    headers=_attachment(f"labcrew_lite_report_{session_id}.pdf", download))
