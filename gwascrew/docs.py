"""Purpose: RAG over the fictional document corpus. Hybrid search, SOP version control, injection quarantine.

How it works, in order:
  1. Each markdown document is split into one chunk per "## " section, so every hit cites doc + section.
  2. Hybrid search: BM25 (exact words, IDs) + FAISS over embeddings (paraphrases), fused by
     Reciprocal Rank Fusion, which combines ranks so two score scales never need calibrating.
  3. Any document matching a prompt-injection pattern is quarantined: never shown to the LLM, reported to the human.
  4. SOP version control in code: use the latest APPROVED version; flag two approved versions that disagree.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

import faiss
import numpy as np
import yaml
from rank_bm25 import BM25Okapi

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"
TOKEN = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")  # keeps "sop-gen-003" and "0.95" whole
RRF_K = 60

# Rules, not an LLM, scan for injections: an LLM scanner could be talked out of its job by the text it scans.
INJECTION = {
    "override_instructions": r"\b(ignore|disregard|forget)\b[^.]{0,40}\b(previous|prior|above|your)\b[^.]{0,20}\b(instructions?|prompt|rules)\b",
    "role_hijack": r"\byou are now\b|\bact as (the )?(approver|admin|system)\b",
    "system_prompt": r"\bsystem prompt\b",
    "bypass_human": r"\bwithout (any )?human (review|approval)\b",
    "force_number": r"\breport (the )?\w+( \w+)? as\b[^.]{0,30}\d",
}


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def injection_flags(text: str) -> list[str]:
    return [name for name, rx in INJECTION.items() if re.search(rx, text, re.I)]


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in str(v).split("."))  # "2.10" > "2.9"


def resolve_sop(sop_id: str, manifest: list[dict]) -> dict:
    """Latest approved version wins; drafts are never used; differing approved versions are a conflict."""
    versions = [m for m in manifest if m.get("sop_id") == sop_id]
    approved = sorted((m for m in versions if m["status"] == "approved"),
                      key=lambda m: (version_key(m["version"]), m["effective_date"]))
    if not approved:
        raise LookupError(f"no approved version of {sop_id}")
    chosen = approved[-1]
    rivals = [m for m in approved[:-1] if m["content_sha256"] != chosen["content_sha256"]]
    return {"sop_id": sop_id, "chosen_doc_id": chosen["doc_id"], "chosen_version": str(chosen["version"]),
            "ignored": [f"{m['doc_id']} ({m['status']})" for m in versions if m is not chosen],
            "conflict": bool(rivals),
            "conflict_detail": (f"conflicting approved versions of {sop_id}: "
                                f"{', '.join('v' + str(m['version']) for m in rivals)} vs v{chosen['version']}; "
                                f"using v{chosen['version']} (latest approved)") if rivals else ""}


class Embedder:
    """bge-small via fastembed (ONNX, no torch). EMBEDDER=lsa gives an offline TF-IDF+SVD stand-in for tests."""

    def __init__(self, texts: list[str]):
        self.kind = os.environ.get("EMBEDDER", "bge")
        if self.kind == "lsa":
            from sklearn.decomposition import TruncatedSVD
            from sklearn.feature_extraction.text import TfidfVectorizer
            self.tfidf = TfidfVectorizer().fit(texts)
            self.svd = TruncatedSVD(n_components=min(64, len(texts) - 1), random_state=0).fit(self.tfidf.transform(texts))
        else:
            from fastembed import TextEmbedding
            cache = Path(__file__).resolve().parents[1] / ".cache" / "fastembed"
            self.model = TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=str(cache))

    def embed(self, texts: list[str]) -> np.ndarray:
        v = (self.svd.transform(self.tfidf.transform(texts)) if self.kind == "lsa"
             else np.array(list(self.model.embed(texts))))
        v = np.asarray(v, dtype="float32")
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)  # inner product = cosine


class Corpus:
    def __init__(self, docs_dir: Path = DOCS_DIR):
        self.manifest = json.loads((docs_dir / "manifest.json").read_text(encoding="utf-8"))
        self.chunks = []
        for entry in self.manifest:
            _, front, body = (docs_dir / entry["file"]).read_text(encoding="utf-8").split("---\n", 2)
            meta = yaml.safe_load(front)
            for block in body.split("\n## ")[1:]:
                heading, _, text = block.partition("\n")
                self.chunks.append({**meta, "version": str(meta.get("version", "")), "section": heading.strip(),
                                    "text": text.strip(), "citation": f"[{meta['doc_id']} §{heading.strip()}]"})
        # One injected section quarantines the WHOLE document: its other sections share the untrusted author.
        self.flagged = {}
        for c in self.chunks:
            if flags := injection_flags(c["text"]):
                self.flagged.setdefault(c["doc_id"], set()).update(flags)
        texts = [f"{c['title']}. {c['section']}. {c['text']}" for c in self.chunks]
        self.bm25 = BM25Okapi([tokenize(t) for t in texts])
        self.embedder = Embedder(texts)
        vecs = self.embedder.embed(texts)
        self.index = faiss.IndexFlatIP(vecs.shape[1])  # exact search
        self.index.add(vecs)
        self.by_key = {(c["doc_id"], c["section"]): c for c in self.chunks}

    def search(self, query: str, k: int = 4) -> dict:
        depth = len(self.chunks)
        _, idx = self.index.search(self.embedder.embed([query]), depth)
        dense = {int(i): r for r, i in enumerate(idx[0])}
        bm25 = {int(i): r for r, i in enumerate(np.argsort(-self.bm25.get_scores(tokenize(query))))}
        fused = sorted(range(depth), key=lambda i: (-(1 / (RRF_K + dense[i]) + 1 / (RRF_K + bm25[i])), i))

        hits, quarantined, resolutions, seen = [], {}, {}, set()
        for i in fused[:k + 4]:  # a few extra: some get quarantined or merged into the current SOP version
            c = self.chunks[i]
            if c["doc_id"] in self.flagged:
                quarantined[c["doc_id"]] = sorted(self.flagged[c["doc_id"]])
                continue
            if c.get("sop_id"):
                res = resolutions.setdefault(c["sop_id"], resolve_sop(c["sop_id"], self.manifest))
                if c["doc_id"] != res["chosen_doc_id"]:  # outdated or draft: swap in the current version
                    c = self.by_key.get((res["chosen_doc_id"], c["section"]))
                    if c is None:
                        continue
            if (c["doc_id"], c["section"]) not in seen and len(hits) < k:
                seen.add((c["doc_id"], c["section"]))
                hits.append({"citation": c["citation"], "doc_id": c["doc_id"], "version": c["version"],
                             "section": c["section"], "text": c["text"]})
        used = {h["doc_id"] for h in hits}
        return {"query": query, "hits": hits,
                "quarantined": [{"doc_id": d, "flags": f} for d, f in quarantined.items()],
                "sop_versions": [r for r in resolutions.values() if r["chosen_doc_id"] in used]}


@lru_cache(maxsize=1)
def corpus() -> Corpus:
    return Corpus()
