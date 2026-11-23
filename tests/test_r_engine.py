"""Independent double programming: the R engine (lm + coloc) and the Python implementation must agree."""
import pytest

from gwascrew.r_stats import analyze_in_r, write_data
from gwascrew.rules import load_rules
from gwascrew.simulate import simulate
from gwascrew.stats import analyze


@pytest.fixture(scope="module")
def both(tmp_path_factory):
    data, rules = simulate(2026), load_rules()
    folder = tmp_path_factory.mktemp("rdata")
    r_res, engine = analyze_in_r(write_data(data, folder), rules, folder)
    return r_res, analyze(data, rules), engine


def test_engine_versions_are_reported(both):
    _, _, engine = both
    assert engine["R"].startswith("4.") and engine["coloc"]


def test_qc_and_study_level_numbers_agree_exactly(both):
    r, py, _ = both
    assert r.qc_failed_by == py.qc_failed_by and r.n_tested == py.n_tested
    assert r.metrics == py.metrics                    # qc_fail_fraction, lambda_gc, n_hits: what the rules judge
    assert r.bonferroni_threshold == py.bonferroni_threshold


def test_per_variant_results_agree(both):
    r, py, _ = both
    assert [h["variant_id"] for h in r.hits] == [h["variant_id"] for h in py.hits]
    assert [x["variant_id"] for x in r.ranked[:10]] == [x["variant_id"] for x in py.ranked[:10]]
    py_rows = {x["variant_id"]: x for x in py.ranked}
    for x in r.ranked:
        y = py_rows.get(x["variant_id"])
        if y:
            for k in ("beta", "se", "p", "pip"):
                assert x[k] == pytest.approx(y[k], rel=2e-3), (x["variant_id"], k)


def test_plot_data_agree(both):
    r, py, _ = both
    assert r.plot["variant_ids"] == py.plot["variant_ids"]
    assert r.plot["neglog10p"] == pytest.approx(py.plot["neglog10p"], abs=2e-3)


def test_r_engine_refuses_a_prior_it_cannot_honour(tmp_path):
    rules = load_rules()
    rules.settings["prior_effect_sd_in_trait_sd"] = 0.2
    with pytest.raises(RuntimeError, match="0.15"):
        analyze_in_r(write_data(simulate(2026), tmp_path), rules, tmp_path)
