"""Tests run fully offline: logs go to a temp folder, retrieval uses the offline LSA embedder."""
import os

import pytest

os.environ.setdefault("EMBEDDER", "lsa")  # EMBEDDER=bge pytest tests/test_docs.py checks real embeddings
os.environ["MOCK_LLM"] = "1"


@pytest.fixture(autouse=True)
def temp_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("GC_LOG_DIR", str(tmp_path / "logs"))


def wide_shortlist(monkeypatch, budget=3):
    """Make every variant shortlist-eligible, so a test can run several rounds (default rules shortlist only 2)."""
    from gwascrew import flow
    from gwascrew.rules import load_rules
    from gwascrew.tools import Session

    def make(**kw):
        rs = load_rules()
        rs.settings.update(assay_budget=budget, assay_min_pip=0.0)
        return Session(ruleset=rs, **kw)
    monkeypatch.setattr(flow, "Session", make)
