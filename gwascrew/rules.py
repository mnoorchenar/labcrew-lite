"""Purpose: load the versioned rules.yaml and turn metrics into a CONFIRM / FAIL / INCONCLUSIVE decision.

Why: pass/fail must come from code plus a versioned config file, never from the LLM. The file's version
and SHA-256 hash are stamped on every decision, so any verdict traces back to the exact rules used.
Aggregation is deliberately conservative: any fail -> FAIL, else any inconclusive -> INCONCLUSIVE, else CONFIRM.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import numpy as np
import yaml
from pydantic import BaseModel, Field

RULES_PATH = Path(__file__).parent / "rules.yaml"
Status = Literal["pass", "fail", "inconclusive"]


class Rule(BaseModel):
    id: str
    kind: Literal["min", "max", "range"]
    metric: str
    stage: Literal["analysis", "confirmation", "item_filter"] = "analysis"
    description: str = ""
    min: float | None = None
    max: float | None = None


class RuleSet(BaseModel):
    version: str
    sha256: str
    settings: dict[str, Any] = Field(default_factory=dict)
    rules: list[Rule]


class RuleResult(BaseModel):
    rule_id: str
    status: Status
    observed: Any
    explanation: str


class Decision(BaseModel):
    outcome: Literal["CONFIRM", "FAIL", "INCONCLUSIVE"]
    reasons: list[str]
    rule_results: list[RuleResult]
    rules_version: str
    rules_sha256: str
    requires_human_approval: bool = True  # nothing proceeds without a human


def load_rules(path: str | Path = RULES_PATH) -> RuleSet:
    raw = Path(path).read_bytes().replace(b"\r\n", b"\n")  # same hash on Windows and Linux
    data = yaml.safe_load(raw)
    return RuleSet(version=str(data["version"]), sha256=hashlib.sha256(raw).hexdigest(),
                   settings=data.get("settings", {}), rules=[Rule(**r) for r in data["rules"]])


def _check(rule: Rule, value: float) -> tuple[Status, str]:
    lo, hi = rule.min, rule.max
    ok = (lo is None or value >= lo) and (hi is None or value <= hi)
    limit = f"[{lo}, {hi}]" if lo is not None and hi is not None else (f">= {lo}" if lo is not None else f"<= {hi}")
    return ("pass" if ok else "fail"), f"{value} {'meets' if ok else 'violates'} {limit}"


def decide(ruleset: RuleSet, metrics: dict[str, Any], stage: str = "analysis") -> Decision:
    results = []
    for rule in (r for r in ruleset.rules if r.stage == stage):
        if rule.metric not in metrics:  # a missing metric is an error, never a silent pass
            raise KeyError(f"rule {rule.id} needs metric '{rule.metric}'")
        status, why = _check(rule, metrics[rule.metric])
        results.append(RuleResult(rule_id=rule.id, status=status, observed=metrics[rule.metric], explanation=why))
    statuses = [r.status for r in results]
    outcome = "FAIL" if "fail" in statuses else "INCONCLUSIVE" if "inconclusive" in statuses else "CONFIRM"
    return Decision(outcome=outcome, reasons=[f"{r.rule_id}: {r.status} ({r.explanation})" for r in results],
                    rule_results=results, rules_version=ruleset.version, rules_sha256=ruleset.sha256)


def vector_pass(rule: Rule, values) -> np.ndarray:
    """Element-wise rule for per-variant QC filters. NaN never passes (unknown is treated as failure)."""
    v = np.asarray(values, dtype=float)
    ok = ~np.isnan(v)
    if rule.min is not None:
        ok &= np.nan_to_num(v, nan=-np.inf) >= rule.min
    if rule.max is not None:
        ok &= np.nan_to_num(v, nan=np.inf) <= rule.max
    return ok
