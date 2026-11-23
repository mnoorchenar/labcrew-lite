"""Purpose: the PYTHON implementation of the GWAS statistics, kept as an independent cross-check of the R engine.

The app runs the statistics in R (stats/gwas.R). This module re-implements them separately (vectorized
numpy), and the tests require both to agree: independent double programming. The methods, in short:
  * QC thresholds come from rules.yaml, so they are versioned, not hard-coded.
  * trait ~ genotype + age + sex for all variants at once: residualise on the covariates once
    (Frisch-Waugh-Lovell), then every slope is a dot product. Validated against statsmodels in the tests.
  * Missing calls are mean-imputed per variant (simple; a stated limitation).
  * Lambda = median(chi-square) / 0.4549 measures inflation of the whole test distribution
    (e.g. population stratification); Bonferroni = 0.05 / number of variants tested after QC.
  * Wakefield's approximate Bayes factor needs only beta and SE; with a stated prior it gives a
    posterior inclusion probability (PIP) used to rank variants for the limited assay budget.
"""
from __future__ import annotations

import numpy as np
from pydantic import BaseModel
from scipy import stats

from gwascrew.rules import RuleSet, vector_pass

CHI2_MEDIAN_1DF = stats.chi2.ppf(0.5, df=1)  # exact, = R's qchisq(0.5, 1)


def sig(x: float, digits: int = 4) -> float:
    """Round to significant figures, so a tiny p-value never prints as 0."""
    return float(f"{x:.{digits}g}")


def variant_qc_metrics(G: np.ndarray) -> dict[str, np.ndarray]:
    """Per-variant MAF, call rate and Hardy-Weinberg p (chi-square, 1 df). NaN = missing call."""
    called = ~np.isnan(G)
    n_called = called.sum(axis=0)
    n0, n1, n2 = ((G == k).sum(axis=0) for k in (0, 1, 2))
    p = (n1 + 2 * n2) / np.maximum(2 * n_called, 1)
    maf = np.minimum(p, 1 - p)
    exp = np.stack([(1 - p) ** 2, 2 * p * (1 - p), p ** 2]) * n_called
    obs = np.stack([n0, n1, n2])
    with np.errstate(divide="ignore", invalid="ignore"):
        chi2 = np.nansum(np.where(exp > 0, (obs - exp) ** 2 / exp, 0.0), axis=0)
    hwe_p = np.where(maf > 0, stats.chi2.sf(chi2, df=1), np.nan)  # monomorphic: untestable
    return {"maf": maf, "call_rate": called.mean(axis=0), "hwe_p": hwe_p}


def association(G: np.ndarray, y: np.ndarray, covars: np.ndarray) -> dict[str, np.ndarray]:
    """Vectorized OLS of y on each genotype column, adjusting for covariates."""
    G = np.where(np.isnan(G), np.nanmean(G, axis=0), G)
    C = np.column_stack([np.ones(len(y)), covars])
    Y = np.column_stack([y, G])
    R = Y - C @ np.linalg.lstsq(C, Y, rcond=None)[0]
    ry, RG = R[:, 0], R[:, 1:]
    sgg = (RG ** 2).sum(axis=0)
    beta = (RG * ry[:, None]).sum(axis=0) / sgg
    df = len(y) - C.shape[1] - 1
    se = np.sqrt(((ry ** 2).sum() - beta ** 2 * sgg) / df / sgg)
    t = beta / se
    return {"beta": beta, "se": se, "t": t, "p": 2 * stats.t.sf(np.abs(t), df)}


def genomic_lambda(t: np.ndarray) -> float:
    return float(np.median(t ** 2) / CHI2_MEDIAN_1DF)


def wakefield_pip(beta: np.ndarray, se: np.ndarray, W: float, prior: float) -> np.ndarray:
    """PIP from Wakefield's ABF: log ABF = 0.5 log(V/(V+W^2)) + 0.5 z^2 W^2/(V+W^2), V = se^2.
    Assumes independent variants (no linkage disequilibrium), which is true in this simulation."""
    V, W2 = se ** 2, W ** 2
    log_abf = 0.5 * np.log(V / (V + W2)) + 0.5 * (beta / se) ** 2 * W2 / (V + W2)
    return 1 / (1 + np.exp(-(log_abf + np.log(prior / (1 - prior)))))


class GwasResult(BaseModel):
    n_samples: int
    n_variants: int
    n_tested: int
    qc_failed_by: dict[str, int]
    qc_fail_fraction: float
    bonferroni_threshold: float
    lambda_gc: float
    n_hits: int
    hits: list[dict]       # Bonferroni hits, best first
    ranked: list[dict]     # all tested variants by PIP, best first (top 20 kept)
    plot: dict             # Manhattan + QQ data for the UI (never sent to the LLM)
    metrics: dict          # exactly what the rules engine sees
    data_label: str = "SIMULATED / FICTIONAL data"


def analyze(data: dict, ruleset: RuleSet) -> GwasResult:
    s = ruleset.settings
    G, y, ids = data["G"], data["trait"], np.asarray(data["variant_ids"])
    qm = variant_qc_metrics(G)
    keep, failed_by = np.ones(len(ids), dtype=bool), {}
    for r in (r for r in ruleset.rules if r.stage == "item_filter"):
        ok = vector_pass(r, qm[r.metric])
        failed_by[r.id] = int((~ok).sum())
        keep &= ok
    res = association(G[:, keep], y, np.column_stack([data["age"], data["sex"]]))
    tested = ids[keep]
    m = int(keep.sum())
    thr = s["family_wise_alpha"] / m
    W = s["prior_effect_sd_in_trait_sd"] * np.std(y, ddof=1)   # coloc convention: 0.15 x SD(trait)
    pip = wakefield_pip(res["beta"], res["se"], W, s["prior_prob_associated"])

    def row(j: int) -> dict:
        return {"variant_id": str(tested[j]), "beta": sig(res["beta"][j]), "se": sig(res["se"][j]),
                "p": sig(res["p"][j], 3), "pip": sig(pip[j], 3), "bonferroni_hit": bool(res["p"][j] < thr)}

    hit_idx = np.where(res["p"] < thr)[0]
    hits = [row(j) for j in hit_idx[np.argsort(res["p"][hit_idx])]]
    ranked = [row(j) for j in np.argsort(-pip)[:20]]
    obs = np.sort(-np.log10(res["p"]))[::-1]
    exp = -np.log10((np.arange(1, m + 1) - 0.5) / m)
    plot = {"position": [int(v[3:]) for v in tested], "neglog10p": np.round(-np.log10(res["p"]), 3).tolist(),
            "qq_expected": np.round(exp, 3).tolist(), "qq_observed": np.round(obs, 3).tolist(),
            "variant_ids": tested.tolist()}
    qc_frac, lam = sig(1 - m / len(ids)), round(genomic_lambda(res["t"]), 4)
    return GwasResult(n_samples=len(y), n_variants=len(ids), n_tested=m, qc_failed_by=failed_by,
                      qc_fail_fraction=qc_frac, bonferroni_threshold=sig(thr), lambda_gc=lam, n_hits=len(hits),
                      hits=hits, ranked=ranked, plot=plot,
                      metrics={"qc_fail_fraction": qc_frac, "lambda_gc": lam, "n_hits": len(hits)})
