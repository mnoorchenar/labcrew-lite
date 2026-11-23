"""Purpose: the CrewAI Flow. It runs the crew each round, applies the code guards, and waits for a human.

    start -> run_round (crew: Analyst + Reviewer) -> guards OK? --no--> needs_review (stop, human must look)
                                                          |yes
                                       human_gate (@human_feedback: approve / reject variants)
                                                          |
                         apply_decision: audit log -> simulated assay (code, never an agent) -> rules
                                                          |
                                     round < 2 ? -> next_round (back to run_round)  :  finish (truth check)
Why a Flow around the crew: the order of steps and the human gate are fixed code, so an agent can never
skip the approval or run the lab itself. The report shown to the human is assembled by code; its hash
goes into the audit log, so we can prove what the reviewer saw when they approved.
"""
from __future__ import annotations

import json
import re
from typing import Any

from crewai.flow.flow import Flow, listen, or_, router, start
from crewai.flow.human_feedback import HumanFeedbackResult, human_feedback
from pydantic import BaseModel, Field

from gwascrew import evidence
from gwascrew.crew import AnalystReport, ReviewVerdict, build_crew
from gwascrew.rules import decide
from gwascrew.simulate import assay_truth, run_assay, truth_check
from gwascrew.tools import Session, invalid_proposals, missing_tools, shortlist

SESSIONS: dict[str, Session] = {}   # live Session objects (numpy data cannot live in Flow state)
FEEDBACK = re.compile(r"^\s*(approve|reject)\s+(all|var\d{4}(?:\s*,\s*var\d{4})*)\s*(?:;\s*(.*))?$", re.I | re.S)


# ---------------------------------------------------------------- human input
class _ProviderProxy:
    """@human_feedback needs its provider when the class is defined; this forwards to the one given to
    each Flow instance (terminal prompt in the CLI, a queue in the web API, a script in tests)."""

    def request_feedback(self, context, flow):
        return flow.feedback_provider.request_feedback(context, flow)


class ScriptedProvider:
    def __init__(self, answers: list[str]):
        self.answers, self.shown = list(answers), []

    def request_feedback(self, context, flow):
        self.shown.append(context.method_output)
        return self.answers.pop(0) if self.answers else "reject all"


class ConsolePrompt:
    def request_feedback(self, context, flow):
        print(context.method_output)
        return input("\nReply 'approve all', 'approve var0012,var0420' or 'reject all' (optional '; comment'): ")


def parse_feedback(text: str, proposals: list[str]) -> tuple[list[str], list[str], str]:
    """Free text -> (approved, rejected, comment). Anything unclear is treated as a rejection, never an approval."""
    m = FEEDBACK.match(text or "")
    if not m:
        return [], proposals, f"unparsed reply treated as reject: {text!r}"
    verb, which, comment = m.group(1).lower(), m.group(2), (m.group(3) or "").strip()
    named = proposals if which.lower() == "all" else [v.strip() for v in which.split(",")]
    named = [v for v in named if v in proposals]          # cannot approve something that was not proposed
    approved = named if verb == "approve" else []
    return approved, [v for v in proposals if v not in approved], comment


# ---------------------------------------------------------------- state + report
class FlowState(BaseModel):
    session_id: str = ""
    question: str = ""
    seed: int = 2026
    rounds_max: int = 2
    reviewer: str = "scientist"
    status: str = "running"   # running | awaiting_approval | needs_review | finished
    stop_reason: str = ""
    rounds: list[dict] = Field(default_factory=list)   # one entry per round: report, review, guards, assay
    truth: dict = Field(default_factory=dict)


def build_report(s: Session, rep: AnalystReport, review: ReviewVerdict | None, guards: dict) -> str:
    d, r = s.decision, s.result
    lines = [f"# GWAS analysis, round {s.round_no}", "", "**SIMULATED / FICTIONAL data**", "",
             f"> Question: {s.question}", "",
             f"## Decision by code: {d.outcome}", f"Rules file version {d.rules_version}.", ""]
    lines += [f"- {x}" for x in d.reasons]
    lines += ["", "## Analyst summary (LLM, every number checked against the tool log)", rep.summary, "",
              "## Proposed for the functional assay (from rank_for_assay)", "| variant | PIP | p | Bonferroni hit |",
              "|---|---|---|---|"]
    by_id = {x["variant_id"]: x for x in r.ranked}
    lines += [f"| {v} | {by_id[v]['pip']} | {by_id[v]['p']} | {by_id[v]['bonferroni_hit']} |" for v in rep.proposed_variants]
    lines += ["", "## Sources retrieved"] + [f"- {x['citation']}" for x in s.sources]
    lines += ["", "## Document control (decided by code)"]
    for sop in s.sop_versions.values():
        lines.append(f"- {sop['sop_id']}: used {sop['chosen_doc_id']} (latest approved); not used: "
                     f"{', '.join(sop['ignored']) or 'none'}")
        if sop["conflict"]:
            lines.append(f"- **SOP CONFLICT, please check:** {sop['conflict_detail']}")
    lines += [f"- **QUARANTINED, possible prompt injection:** {doc} ({', '.join(flags)}); "
              "hidden from the agents" for doc, flags in s.quarantined.items()]
    if not s.sop_versions and not s.quarantined:
        lines.append("- no SOP version decisions or quarantined documents in this round's searches")
    lines += ["", "## Checks",
              f"- numbers in the summary match this session's tool log: {guards['numbers_ok']}",
              f"- required tools ran: {not guards['missing_tools']}",
              f"- proposals all came from the ranking tool: {not guards['invalid_proposals']}",
              f"- Reviewer agent: {'supported' if review and review.supported else 'NOT supported'}"
              + (f" ({'; '.join(review.issues)})" if review and review.issues else "")]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- the flow
class GwasFlow(Flow[FlowState]):
    def __init__(self, feedback_provider=None, mock=None, **kw):
        super().__init__(**kw)
        self.feedback_provider, self.mock = feedback_provider or ConsolePrompt(), mock

    @property
    def s(self) -> Session:
        return SESSIONS[self.state.session_id]

    @start()
    def begin(self):
        s = Session(question=self.state.question, seed=self.state.seed)
        if self.state.session_id:          # the API may choose the id up front
            s.id = self.state.session_id
        SESSIONS[s.id] = s
        self.state.session_id = s.id

    @listen(or_(begin, "next_round"))
    def run_round(self):
        s = self.s
        s.sources, s.decision, s.proposals, s.quarantined, s.sop_versions = [], None, [], {}, {}
        history = ""
        if self.state.rounds:
            last = self.state.rounds[-1].get("assay", [])
            results = ", ".join(f"{a['variant_id']} {'positive' if a['assay_positive'] else 'negative'}" for a in last)
            history = (f"Round {s.round_no - 1} functional assay results: {results}. "
                       "rank_for_assay skips variants that were already assayed.")
        crew, analysis, review = build_crew(s, history, self.mock)
        entry: dict[str, Any] = {"round": s.round_no}
        try:
            crew.kickoff()
            rep = AnalystReport.model_validate_json(analysis.output.raw)
            verdict = ReviewVerdict.model_validate_json(review.output.raw)
        except Exception as exc:  # guardrail exhausted its retries, provider down, ...: fail closed
            entry.update(error=f"{type(exc).__name__}: {str(exc)[:600]}")
            self.state.rounds.append(entry)
            return
        guards = {"missing_tools": missing_tools(s), "invalid_proposals": invalid_proposals(s, rep.proposed_variants),
                  "numbers_ok": evidence.check_numbers(rep.summary, s.id)["ok"]}
        report = build_report(s, rep, verdict, guards)
        r = s.result
        by_id = {x["variant_id"]: x for x in r.ranked}
        entry.update(summary=rep.summary, proposals=rep.proposed_variants, review=verdict.model_dump(),
                     guards=guards, decision=s.decision.model_dump(), report_md=report,
                     report_sha256=evidence.sha256(report),
                     # for the page: headline numbers, proposal details, sources and document-control decisions
                     gwas={"n_variants": r.n_variants, "n_tested": r.n_tested, "qc_failed_by": r.qc_failed_by,
                           "lambda_gc": r.lambda_gc, "bonferroni_threshold": r.bonferroni_threshold,
                           "n_hits": r.n_hits, "hits": r.hits, "engine": s.engine},
                     proposal_rows=[by_id[v] for v in rep.proposed_variants if v in by_id],
                     sources=list(s.sources), sop_versions=list(s.sop_versions.values()),
                     quarantined=[{"doc_id": d, "flags": f} for d, f in s.quarantined.items()],
                     analyst_attempts=sum(c["tool"] == "run_gwas" and c["inputs"].get("round") == s.round_no
                                          for c in evidence.tool_calls(s.id)))
        self.state.rounds.append(entry)

    @router(run_round)
    def guards_ok(self):
        e = self.state.rounds[-1]
        g = e.get("guards")
        if "error" in e or not g or g["missing_tools"] or g["invalid_proposals"] or not g["numbers_ok"]:
            self.state.status = "needs_review"
            self.state.stop_reason = e.get("error") or f"code guards failed: {g}"
            return "needs_review"
        self.state.status = "awaiting_approval"
        return "ready"

    @human_feedback(message="Approve variants for the functional assay?", provider=_ProviderProxy())
    @listen("ready")
    def human_gate(self):
        return self.state.rounds[-1]["report_md"]   # exactly what the human sees (its hash is audited)

    @router(human_gate)
    def apply_decision(self, result: HumanFeedbackResult):
        s, e = self.s, self.state.rounds[-1]
        approved, rejected, comment = parse_feedback(result.feedback, e["proposals"])
        e["human"] = evidence.record_human_decision(s.id, s.round_no, self.state.reviewer, approved, rejected,
                                                    comment, e["decision"], e["report_sha256"])
        self.state.status = "running"
        if not approved:
            self.state.stop_reason = "the reviewer approved no variants"
            return "finished"
        assay = run_assay(approved, seed=s.seed + s.round_no)           # the simulated lab: code only
        n_pos = sum(a["assay_positive"] for a in assay)
        s.log("run_assay", {"variants": approved}, {"results": assay, "n_assay_positive": n_pos})
        s.assayed += approved
        e["assay"] = assay
        e["assay_decision"] = decide(s.ruleset, {"n_assay_positive": n_pos}, stage="confirmation").model_dump()
        if s.round_no < self.state.rounds_max:
            if not shortlist(s):   # every variant that meets the shortlist rule has been assayed: nothing to propose
                self.state.stop_reason = "no remaining variants meet the shortlist rule (Bonferroni hit or PIP >= assay_min_pip)"
                return "finished"
            s.round_no += 1
            return "next_round"
        self.state.stop_reason = f"completed {self.state.rounds_max} rounds"
        return "finished"

    @listen("finished")
    def finish(self):
        s = self.s
        positives = [a["variant_id"] for r in self.state.rounds for a in r.get("assay", []) if a["assay_positive"]]
        truth = truth_check([h["variant_id"] for h in s.result.hits], s.result.plot["variant_ids"])
        truth.update(assay_truth(positives))
        s.log("truth_check", {}, truth)   # only possible because the data are simulated
        self.state.truth, self.state.status = truth, "finished"

    @listen("needs_review")
    def stop_for_review(self):
        self.state.status = "needs_review"


def run_flow(question: str, provider=None, seed: int = 2026, rounds: int = 2, reviewer: str = "scientist",
             session_id: str = "", mock=None) -> GwasFlow:
    flow = GwasFlow(feedback_provider=provider, mock=mock, suppress_flow_events=True, tracing=False)
    flow.kickoff(inputs={"question": question, "seed": seed, "rounds_max": rounds, "reviewer": reviewer,
                         "session_id": session_id})
    return flow


if __name__ == "__main__":   # python -m gwascrew.flow "Which variants are associated with the trait?"
    import sys
    f = run_flow(" ".join(sys.argv[1:]) or "Which variants are associated with the trait?")
    print(f"\nStatus: {f.state.status} ({f.state.stop_reason})\nTruth check: {f.state.truth}")
