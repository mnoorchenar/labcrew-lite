"""Rules engine: versioned, hashed, conservative, and a missing metric is an error."""
import pytest

from gwascrew.rules import decide, load_rules

GOOD = {"qc_fail_fraction": 0.08, "lambda_gc": 1.0, "n_hits": 3}


def test_rules_are_versioned_and_hashed():
    rs = load_rules()
    assert rs.version == "1.2.0" and len(rs.sha256) == 64


def test_any_fail_means_fail():
    rs = load_rules()
    assert decide(rs, GOOD).outcome == "CONFIRM"
    assert decide(rs, {**GOOD, "lambda_gc": 1.2}).outcome == "FAIL"
    assert decide(rs, {**GOOD, "n_hits": 0}).outcome == "FAIL"


def test_missing_metric_is_an_error_not_a_pass():
    with pytest.raises(KeyError):
        decide(load_rules(), {"lambda_gc": 1.0})


def test_assay_confirmation_rule():
    assert decide(load_rules(), {"n_assay_positive": 0}, stage="confirmation").outcome == "FAIL"
