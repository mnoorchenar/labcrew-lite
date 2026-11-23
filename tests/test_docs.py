"""RAG: current approved SOP version wins, drafts are never used, conflicts and injections are flagged."""
from gwascrew.docs import corpus, injection_flags, resolve_sop


def test_latest_approved_sop_is_used_and_conflict_flagged():
    res = resolve_sop("SOP-GEN-001", corpus().manifest)
    assert res["chosen_doc_id"] == "SOP-GEN-001-v2.0"     # v2.1 is a draft
    assert res["conflict"]                                  # v1.0 and v2.0 are both approved and differ


def test_search_never_returns_draft_or_old_sop():
    out = corpus().search("variant call rate filter quality control")
    ids = {h["doc_id"] for h in out["hits"]}
    assert "SOP-GEN-001-v2.1" not in ids and "SOP-GEN-001-v1.0" not in ids
    assert all(h["citation"].startswith("[") and "§" in h["citation"] for h in out["hits"])


def test_injection_document_is_quarantined():
    out = corpus().search("var0333 FICT11 annotation prior evidence")
    assert "ANN-INJ-01" not in {h["doc_id"] for h in out["hits"]}
    assert "ANN-INJ-01" in {q["doc_id"] for q in out["quarantined"]}


def test_normal_sop_language_is_not_flagged():
    assert injection_flags("Do not ignore deviations; report them to QA.") == []
