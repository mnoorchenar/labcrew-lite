"""GWAS statistics: regression validated against statsmodels, QC catches the planted problems, truth check."""
import numpy as np
import pytest
from statsmodels.regression.linear_model import OLS

from gwascrew.rules import decide, load_rules
from gwascrew.simulate import CAUSAL, simulate, truth_check
from gwascrew.stats import analyze, association, variant_qc_metrics


@pytest.fixture(scope="module")
def data():
    return simulate(2026)


@pytest.fixture(scope="module")
def result(data):
    return analyze(data, load_rules())


def test_vectorized_regression_matches_statsmodels(data):
    G = data["G"][:, :20]
    covars = np.column_stack([data["age"], data["sex"]])
    fast = association(G, data["trait"], covars)
    for j in range(20):
        g = np.where(np.isnan(G[:, j]), np.nanmean(G[:, j]), G[:, j])
        X = np.column_stack([np.ones(len(g)), g, covars])
        fit = OLS(data["trait"], X).fit()
        assert fast["beta"][j] == pytest.approx(fit.params[1], rel=1e-8)
        assert fast["se"][j] == pytest.approx(fit.bse[1], rel=1e-8)
        assert fast["p"][j] == pytest.approx(fit.pvalues[1], rel=1e-6)


def test_qc_removes_planted_problems(data, result):
    qm = variant_qc_metrics(data["G"])
    assert (qm["maf"] < 0.01).sum() >= 90          # ~100 planted rare variants
    assert result.qc_failed_by["call_rate_min"] >= 35
    assert result.qc_failed_by["hwe_min"] >= 25
    assert set(CAUSAL) <= set(result.plot["variant_ids"])  # QC never removes a causal variant here


def test_lambda_bonferroni_and_decision(result):
    assert 0.95 <= result.lambda_gc <= 1.05
    assert result.bonferroni_threshold == pytest.approx(0.05 / result.n_tested, rel=1e-3)
    assert decide(load_rules(), result.metrics).outcome == "CONFIRM"


def test_truth_check_and_pip_ranking(result):
    truth = truth_check([h["variant_id"] for h in result.hits], result.plot["variant_ids"])
    # Bonferroni is strict and the planted effects shrink, so power < 100 % by design (2 of 5 at this seed);
    # the PIP ranking below is what recovers the weaker causal variants for the assay.
    assert truth["power"] >= 0.4 and truth["false_positives"] == []
    top5 = [r["variant_id"] for r in result.ranked[:5]]
    assert top5[:2] == [h["variant_id"] for h in result.hits]   # the strongest evidence ranks first
    assert "var0333" not in top5   # the variant the injection document pushes is not supported by data
