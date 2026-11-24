"""Tools and guards: session-scoped logging, number check, required tools, proposal validation."""
import pytest

from gwascrew import evidence
from gwascrew.tools import Session, analyst_tools, invalid_proposals, missing_tools, reviewer_tools


def run_all(s):
    run_gwas, check_rules, rank, search = analyst_tools(s)
    return run_gwas.run(), check_rules.run(), rank.run(), search.run(query="Bonferroni threshold")


def test_tools_log_with_session_id_and_set_decision():
    s = Session(question="q")
    gwas, rules, rank, _ = run_all(s)
    assert rules["outcome"] == s.decision.outcome == "CONFIRM"
    assert 1 <= len(rank["candidates"]) <= s.ruleset.settings["assay_budget"]
    assert all(c["bonferroni_hit"] or c["pip"] >= s.ruleset.settings["assay_min_pip"] for c in rank["candidates"])
    assert {r["tool"] for r in evidence.tool_calls(s.id)} == {"run_gwas", "check_rules", "rank_for_assay", "search_docs"}
    assert evidence.tool_calls("other-session") == []


def test_rules_tool_refuses_before_analysis():
    s = Session(question="q")
    assert "error" in analyst_tools(s)[1].run()
    assert s.decision is None


def test_number_check_accepts_tool_numbers_and_rejects_invented_ones():
    s = Session(question="q")
    gwas, *_ = run_all(s)
    verify = reviewer_tools(s)[0]
    good = f"Lambda was {gwas['lambda_gc']} with {gwas['n_hits']} hits below {gwas['bonferroni_threshold']}."
    assert verify.run(report=good)["numbers"]["ok"]
    bad = verify.run(report="var0333 reached p = 1e-20.")["numbers"]
    assert not bad["ok"] and bad["unmatched"] == ["1e-20"]   # variant id var0333 is not a number


@pytest.mark.parametrize("text, expected", [
    ("threshold 2.728e-05.", ["2.728e-05"]), ("p = 2.999e-20 (run_gwas)", ["2.999e-20"]), ("p<1E+3,", ["1E+3"]),
    ("beta -0.261", ["-0.261"]), (".777", [".777"]), ("7.77-8.88", ["7.77", "8.88"]), ("7.77-fold", ["7.77"]),
    ("7.77x", ["7.77"]), ("λ0.9999", ["0.9999"]), ("R 4.6.2", ["4.6.2"]),
    ("var0420 in [SOP-GEN-003-v1.0 §Thresholds], v2.0", []),
])
def test_number_tokens(text, expected):
    assert evidence.number_tokens(text) == expected


def test_invented_numbers_are_rejected_in_every_written_form():
    s = Session(question="q")
    run_all(s)
    for bad in ("2.999e-20", "2.729e-05", ".777", "7.77-8.88", "7.77-fold", "7.77x", "λ0.9999"):
        assert not evidence.check_numbers(f"The value was {bad} (run_gwas).", s.id)["ok"], bad
    for good in ("2.728e-05", "0.00002728", "1.79e-09", "1.41e-7", "0.9732"):
        assert evidence.check_numbers(f"The value was {good} (run_gwas).", s.id)["ok"], good


def test_a_correct_scientific_number_passes_when_its_first_digit_is_unknown():
    s = Session(question="q")
    s.log("run_gwas", {}, {"p": 7.123e-09})
    assert evidence.check_numbers("p = 7.123e-09", s.id)["ok"]


def test_numbers_from_another_session_do_not_count():
    a, b = Session(question="q"), Session(question="q")
    gwas, *_ = run_all(a)
    assert not evidence.check_numbers(f"lambda {gwas['lambda_gc']}", b.id)["ok"]


def test_missing_tools_and_invalid_proposals():
    s = Session(question="q")
    assert missing_tools(s) == ["run_gwas", "check_rules", "rank_for_assay"]
    run_all(s)
    assert missing_tools(s) == []
    assert invalid_proposals(s, s.proposals) == []
    assert invalid_proposals(s, ["var0333"]) == ["var0333"]


def test_second_round_skips_assayed_variants():
    s = Session(question="q")
    _, _, first, _ = run_all(s)
    s.assayed, s.round_no = [c["variant_id"] for c in first["candidates"]], 2
    second = analyst_tools(s)[2].run()
    assert not {c["variant_id"] for c in second["candidates"]} & set(s.assayed)


def test_checking_a_bad_number_twice_never_whitelists_it():
    s = Session(question="q")
    run_all(s)
    verify = reviewer_tools(s)[0]
    assert not verify.run(report="var0333 reached p = 1e-20.")["numbers"]["ok"]
    assert not verify.run(report="var0333 reached p = 1e-20.")["numbers"]["ok"]   # the check's own log is not evidence


def test_big_data_files_are_removed_after_the_r_run(tmp_path):
    from gwascrew import evidence
    from gwascrew.tools import Session, analyst_tools
    s = Session(question="q")
    run_gwas = analyst_tools(s)[0]
    run_gwas.run()
    folder = evidence.log_dir() / "data" / s.id
    assert (folder / "r_output.json").exists() and not (folder / "genotypes.bin").exists()


# ---- Groq "tool_use_failed": gpt-oss sometimes calls its channel name ('commentary') instead of the tool
def _groq_error(name, args):
    import json
    gen = json.dumps({"name": name, "arguments": args})
    body = json.dumps({"error": {"message": f"Tool call validation failed: attempted to call tool '{name}' which was not in request.tools",
                                 "type": "invalid_request_error", "code": "tool_use_failed", "failed_generation": gen}})
    return Exception(f"litellm.BadRequestError: GroqException - {body}")


def _schemas():
    from crewai.utilities.agent_utils import convert_tools_to_openai_schema
    from gwascrew.crew import submit_report, submit_review
    return convert_tools_to_openai_schema([submit_report, submit_review])[0]


def test_misnamed_submit_report_is_repaired_to_the_real_tool():
    import json
    from gwascrew.crew import failed_tool_call, repair_tool_call
    args = {"proposed_variants": ["var0012", "var0420"], "summary": "The scan tested 1833 variants (run_gwas)."}
    bad = failed_tool_call(_groq_error("commentary", args))
    assert bad == {"name": "commentary", "arguments": args}
    fixed = repair_tool_call(bad, _schemas())
    assert fixed[0]["function"]["name"] == "submit_report" and json.loads(fixed[0]["function"]["arguments"]) == args


def test_tool_repair_never_guesses():
    from gwascrew.crew import failed_tool_call, repair_tool_call
    schemas = _schemas()
    assert repair_tool_call({"name": "commentary", "arguments": {"summary": "x"}}, schemas) is None        # missing a required field
    assert repair_tool_call({"name": "commentary", "arguments": {"summary": "x", "proposed_variants": [], "extra": 1}}, schemas) is None
    assert repair_tool_call({"name": "submit_report", "arguments": {"summary": "x"}}, schemas) is None     # real tool, bad args
    assert failed_tool_call(Exception("Rate limit reached for model")) is None                          # other errors untouched


def test_unrepairable_call_is_retried_once_with_the_tool_names():
    from gwascrew.crew import CachedLLM

    class FakeLLM:
        def __init__(self): self.seen = []
        def call(self, msgs, **kw):
            self.seen.append(msgs); return [{"id": "c1", "type": "function", "function": {"name": "submit_review", "arguments": "{}"}}]
    fake = FakeLLM()
    raw, source = CachedLLM._recover_tool_call(fake, {"name": "json", "arguments": {"foo": 1}}, [{"role": "user", "content": "q"}],
                                               _schemas(), ["submit_report", "submit_review"], None, None)
    assert source == "llm-retried" and "no tool named 'json'" in fake.seen[0][-1]["content"]
