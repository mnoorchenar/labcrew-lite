"""Tools and guards: session-scoped logging, number check, required tools, proposal validation."""
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
