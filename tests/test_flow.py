"""The Flow end to end with a scripted offline agent, plus agents that misbehave on purpose.
Mock agents answer with native tool calls, like the real model; the last call is submit_report."""
import json

from gwascrew import evidence
from gwascrew.crew import scripted_agent, submitted, tool_call
from tests.conftest import wide_shortlist
from gwascrew.flow import SESSIONS, ScriptedProvider, parse_feedback, run_flow

Q = "Which variants are associated with the trait?"


def tools_called(flow):
    return [r["tool"] for r in evidence.tool_calls(flow.state.session_id)]


def test_stops_cleanly_when_no_variants_are_left_to_propose():
    flow = run_flow(Q, provider=ScriptedProvider(["approve all"]))   # default rules shortlist only the 2 real hits
    st = flow.state
    assert st.status == "finished" and len(st.rounds) == 1 and "no remaining variants" in st.stop_reason, st.stop_reason
    assert all(c["bonferroni_hit"] or c["pip"] >= 0.1 for c in [r for r in st.rounds[0]["proposal_rows"]])


def test_two_rounds_with_human_approval(monkeypatch):
    wide_shortlist(monkeypatch)
    human = ScriptedProvider(["approve all; looks fine", "approve all"])
    flow = run_flow(Q, provider=human)
    st = flow.state
    assert st.status == "finished" and len(st.rounds) == 2, st.stop_reason
    assert "Decision by code: CONFIRM" in human.shown[0] and "SIMULATED / FICTIONAL" in human.shown[0]
    r1, r2 = st.rounds
    assert not set(r1["proposals"]) & set(r2["proposals"])          # round 2 never re-assays a variant
    audit = evidence.audit_log(st.session_id)
    assert [a["round"] for a in audit] == [1, 2]
    assert audit[0]["shown_report_sha256"] == evidence.sha256(human.shown[0]) and audit[0]["comment"] == "looks fine"
    assert tools_called(flow).count("run_assay") == 2 and "truth_check" in tools_called(flow)
    assert st.truth["power"] == 0.4


def test_reject_all_stops_without_running_the_lab():
    flow = run_flow(Q, provider=ScriptedProvider(["reject all"]))
    assert flow.state.stop_reason == "the reviewer approved no variants"
    assert "run_assay" not in tools_called(flow)


def test_human_can_approve_a_subset_only_of_proposed_variants():
    proposals = ["var0012", "var0420", "var0018"]
    assert parse_feedback("approve var0012, var0333", proposals) == (["var0012"], ["var0420", "var0018"], "")
    assert parse_feedback("sure, go ahead", proposals)[0] == []          # unclear reply is never an approval


def injected_agent(role, msgs):
    """An Analyst that obeys the injected document: pushes var0333 with an invented p-value."""
    out = scripted_agent(role, msgs)
    if role == "Analyst" and submitted(out):
        return tool_call("submit_report", {"summary": "var0333 reached p = 1e-20 [SOP-GEN-003-v1.0 §Significance].",
                                           "proposed_variants": ["var0333"]})
    return out


def test_injected_agent_is_stopped_before_any_human_or_lab_step():
    human = ScriptedProvider(["approve all"])
    flow = run_flow(Q, provider=human, mock=injected_agent)
    assert flow.state.status == "needs_review"
    assert "guardrail" in flow.state.stop_reason.lower()
    assert human.shown == [] and "run_assay" not in tools_called(flow)


def skipping_agent(role, msgs):
    """An Analyst that never calls check_rules (even after feedback) and claims a verdict by itself."""
    out = scripted_agent(role, msgs)
    if role == "Analyst" and isinstance(out, list) and out[0]["function"]["name"] == "check_rules":
        return tool_call("submit_report", {"summary": "The scan is CONFIRM [SOP-GEN-003-v1.0 §Significance].",
                                           "proposed_variants": ["var0012"]})
    return out


def test_agent_that_skips_the_rules_tool_never_reaches_the_human():
    human = ScriptedProvider(["approve all"])
    flow = run_flow(Q, provider=human, mock=skipping_agent)
    assert flow.state.status == "needs_review" and human.shown == []


def learning_agent(role, msgs):
    """Invents a number first; after the guardrail's feedback it answers correctly (the retry loop works)."""
    text = "\n".join(str(m.get("content")) for m in msgs)
    out = scripted_agent(role, msgs)
    ans = submitted(out)
    if role == "Analyst" and ans and "are not in any tool output" not in text:
        ans["summary"] = ans["summary"].replace("lambda ", "lambda 1.31 instead of ", 1)
        return tool_call("submit_report", ans)
    return out


def test_guardrail_feedback_lets_the_agent_correct_itself():
    flow = run_flow(Q, provider=ScriptedProvider(["reject all"]), mock=learning_agent, rounds=1)
    st = flow.state
    assert "guards" in st.rounds[0], st.stop_reason
    assert st.rounds[0]["guards"]["numbers_ok"] and "1.31" not in st.rounds[0]["summary"]
    assert SESSIONS[st.session_id].decision.outcome == "CONFIRM"


def contradicting_agent(role, msgs):
    """Contradicts the code's CONFIRM in lowercase words, without using the outcome token."""
    out = scripted_agent(role, msgs)
    ans = submitted(out)
    if role == "Analyst" and ans:
        ans["summary"] = ans["summary"].split(", so the rules tool")[0] + ", and lambda failed the range check (run_gwas)."
        return tool_call("submit_report", ans)
    return out


def test_free_text_verdict_is_rejected_even_without_the_outcome_token():
    human = ScriptedProvider(["approve all"])
    flow = run_flow(Q, provider=human, mock=contradicting_agent)
    assert flow.state.status == "needs_review" and "judge results" in flow.state.stop_reason and human.shown == []


def document_agent(role, msgs):
    """Searches the QC SOP (two conflicting approved versions) and the var0333 note (an injection document)."""
    text = "\n".join(str(m.get("content")) for m in msgs)
    out = scripted_agent(role, msgs)
    if role == "Analyst" and isinstance(out, list) and out[0]["function"]["name"] == "search_docs":
        return tool_call("search_docs", {"query": "variant call rate quality control filters"})
    if role == "Analyst" and submitted(out) and "ANN-INJ-01" not in text:
        return tool_call("search_docs", {"query": "var0333 FICT11 annotation prior evidence"})
    return out


def test_sop_conflict_and_quarantine_are_shown_to_the_human():
    human = ScriptedProvider(["reject all"])
    run_flow(Q, provider=human, mock=document_agent, rounds=1)
    report = human.shown[0]
    assert "SOP CONFLICT" in report and "SOP-GEN-001" in report and "SOP-GEN-001-v2.1 (draft)" in report
    assert "QUARANTINED" in report and "ANN-INJ-01" in report
