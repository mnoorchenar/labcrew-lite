"""Purpose: the evidence trail. A session-scoped tool log, the human audit log, and the number check.

Why: hard rule 3 says every number in a report must exactly match a tool output. Every tool call is
written as one JSON line stamped with its session id, so the check only accepts numbers produced in
THIS session (an append-only log would otherwise let yesterday's numbers pass today's check).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


def log_dir() -> Path:
    p = Path(os.environ.get("GC_LOG_DIR", Path(__file__).resolve().parents[1] / "logs"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return jsonable(obj.model_dump())
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


_LOCK = threading.Lock()   # session threads append concurrently; the page polls reads every 1.5 s
_INDEX: dict[str, dict] = {}   # log path -> {"offset": bytes parsed so far, "rows": {session_id: [records]}}


def _append(name: str, record: dict) -> None:
    line = json.dumps(record, default=str) + "\n"
    with _LOCK, open(log_dir() / name, "a", encoding="utf-8") as f:
        f.write(line)


def _read(name: str, session_id: str) -> list[dict]:
    """Rows of one session. The logs are append-only, so only the bytes added since the last read are parsed:
    a poll stays fast however large the log grows."""
    path = log_dir() / name
    key = str(path)
    with _LOCK:
        if not path.exists():
            _INDEX.pop(key, None)
            return []
        idx = _INDEX.get(key)
        size = path.stat().st_size
        if idx is None or size < idx["offset"]:   # first read, or the file was replaced / truncated
            idx = _INDEX[key] = {"offset": 0, "rows": {}}
        if size > idx["offset"]:
            with open(path, "rb") as f:
                f.seek(idx["offset"])
                chunk = f.read()
            end = chunk.rfind(b"\n") + 1   # parse complete lines only; a half-written line waits for the next read
            for raw in chunk[:end].splitlines():
                if raw.strip():
                    rec = json.loads(raw.decode("utf-8"))
                    idx["rows"].setdefault(rec.get("session_id"), []).append(rec)
            idx["offset"] += end
        return list(idx["rows"].get(session_id, []))


def log_tool_call(session_id: str, tool: str, version: str, inputs: dict, output: Any, status: str = "ok") -> None:
    import scipy
    inputs = jsonable(inputs)
    _append("tool_calls.jsonl", {
        "timestamp": now(), "session_id": session_id, "tool": tool, "tool_version": version,
        "library_versions": {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__},
        "inputs": inputs, "inputs_sha256": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
        "status": status, "output": jsonable(output)})


def tool_calls(session_id: str) -> list[dict]:
    return _read("tool_calls.jsonl", session_id)


def record_human_decision(session_id: str, round_no: int, reviewer: str, approved: list[str],
                          rejected: list[str], comment: str, decision: dict, shown_sha256: str) -> dict:
    """Audit log: who decided what, on which rules version, after seeing exactly which text."""
    rec = {"timestamp": now(), "session_id": session_id, "round": round_no, "reviewer": reviewer,
           "approved": approved, "rejected": rejected, "comment": comment,
           "code_decision": decision.get("outcome"), "rules_version": decision.get("rules_version"),
           "rules_sha256": decision.get("rules_sha256"), "shown_report_sha256": shown_sha256}
    _append("audit.jsonl", rec)
    return rec


def audit_log(session_id: str) -> list[dict]:
    return _read("audit.jsonl", session_id)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ number check
# IDs such as var0420, SOP-GEN-003-v1.0 or v2.0 start with a letter and contain a digit; they are removed first.
# Anything that starts with a digit (2.728e-05, 7.77-fold, 7.77x) is never an ID, so it is always checked.
# ASCII letters only: "λ0.9" is a Greek symbol followed by a number, not an ID.
ID_TOKEN = re.compile(r"(?<![A-Za-z0-9_.\-])(?=[A-Za-z_.\-]*\d)[A-Za-z][A-Za-z0-9_.\-]*[A-Za-z0-9_]")
# Versions (4.6.2) stay whole; otherwise integers, decimals (also .777), scientific notation. A minus sign counts
# only when it does not follow a number or word, so the range 7.77-8.88 is two positive numbers.
NUMBER = re.compile(r"(?:(?<![A-Za-z0-9_.])-)?(?<![A-Za-z0-9_.])"
                    r"(?:\d+(?:\.\d+){2,}|(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?%?)")


def number_tokens(text: str) -> list[str]:
    return NUMBER.findall(ID_TOKEN.sub(" ", text))


def canon(token: str) -> str:
    """One canonical form per number, so 0.0250, 2.5e-2 and 0.025 all compare equal."""
    if token.count(".") > 1:   # a version such as 4.6.2: compared as text
        return token
    x = float(token.rstrip("%"))
    return str(int(x)) if x.is_integer() else repr(x)


def numbers_in(obj: Any, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(canon(repr(float(obj))))
    elif isinstance(obj, str):
        out.update(canon(n) for n in number_tokens(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            numbers_in(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            numbers_in(v, out)
    return out


# Only these tools produce evidence. verify_report is NOT here: its own output (counts, the unmatched numbers it
# found) must never become "known", or checking a bad number once would whitelist it for the second check.
EVIDENCE_TOOLS = {"run_gwas", "check_rules", "rank_for_assay", "search_docs", "run_assay", "truth_check"}


def check_numbers(text: str, session_id: str) -> dict:
    """Return every number in `text` that no evidence-producing tool call in this session produced."""
    known: set[str] = set()
    for rec in tool_calls(session_id):
        if rec["status"] == "ok" and rec["tool"] in EVIDENCE_TOOLS:
            numbers_in(rec["output"], known)
    found = number_tokens(text)
    unmatched = sorted({n for n in found if canon(n) not in known})
    return {"ok": not unmatched, "checked": len(found), "unmatched": unmatched}
