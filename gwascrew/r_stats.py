"""Purpose: the bridge from Python to the R statistics engine (stats/gwas.R). JSON in, JSON out.

Why a subprocess: each call runs `Rscript stats/gwas.R in.json out.json`. There is no R server to manage,
and R stays a pure statistics engine that knows nothing about agents or LLMs. The data are written once per
session as plain files (the genotype matrix as raw float64, covariates as CSV), so R reads exactly what
Python simulated. R's own version and coloc's version come back with every result and are logged.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from gwascrew.rules import RuleSet
from gwascrew.stats import GwasResult

ROOT = Path(__file__).resolve().parents[1]
R_SCRIPT = ROOT / "stats" / "gwas.R"


def rscript() -> str:
    """RSCRIPT env var, else Rscript on PATH, else the newest standard Windows install."""
    found = os.environ.get("RSCRIPT") or shutil.which("Rscript") or \
        next(iter(sorted(glob.glob(r"C:\Program Files\R\R-*\bin\Rscript.exe"), reverse=True)), None)
    if not found:
        raise RuntimeError("Rscript not found: install R or set RSCRIPT to its path")
    return found


def write_data(data: dict, folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    G = np.asarray(data["G"], dtype="float64")
    G.T.tofile(folder / "genotypes.bin")   # transposed bytes = column-major order, which R's matrix() expects
    pd.DataFrame({"age": data["age"], "sex": data["sex"], "trait": data["trait"]}).to_csv(
        folder / "covariates.csv", index=False)
    return {"genotypes_bin": str(folder / "genotypes.bin"), "covariates_csv": str(folder / "covariates.csv"),
            "n": int(G.shape[0]), "m": int(G.shape[1]), "variant_ids": [str(v) for v in data["variant_ids"]]}


def analyze_in_r(files: dict, ruleset: RuleSet, folder: Path) -> tuple[GwasResult, dict]:
    s = ruleset.settings
    qc_rules = [{"id": r.id, "metric": r.metric, "min": r.min, "max": r.max}
                for r in ruleset.rules if r.stage == "item_filter"]
    request = {**files, "qc_rules": qc_rules, "family_wise_alpha": s["family_wise_alpha"],
               "prior_prob_associated": s["prior_prob_associated"],
               "prior_effect_sd_in_trait_sd": s["prior_effect_sd_in_trait_sd"]}
    inp, out = folder / "r_input.json", folder / "r_output.json"
    inp.write_text(json.dumps(request), encoding="utf-8")
    proc = subprocess.run([rscript(), str(R_SCRIPT), str(inp), str(out)], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"R engine failed: {proc.stderr.strip()[-500:]}")
    res = json.loads(out.read_text(encoding="utf-8"))
    engine = res.pop("engine")
    return GwasResult(**res), engine
