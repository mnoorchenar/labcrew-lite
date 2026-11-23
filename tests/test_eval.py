"""The evaluation harness itself: it runs, scores the safety cases, and writes its results."""
from eval.run_eval import main


def test_harness_scores_injection_and_sop_questions(tmp_path):
    res = main(["G02", "G05"], out=tmp_path)
    s = res["summary"]
    assert s["reached_human"] == "2/2" and s["numbers_exact_match"] == "2/2"
    assert s["correct_sop_version"] == "1/1" and s["sop_conflict_shown_to_human"] == "1/1"
    assert s["injection_quarantined"] == "1/1" and s["injected_variant_not_proposed"] == "1/1"
    assert (tmp_path / "mock.json").exists() and (tmp_path / "mock.md").exists()
