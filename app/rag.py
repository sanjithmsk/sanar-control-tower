"""Retrieval over the knowledge base: SOPs, policies and supplier agreements.

knowledge/common/ applies to every domain; knowledge/<domain>/ holds that industry's documents.
Ranking is BM25 with light plural folding, dependency-free so the project runs anywhere. To move
to a vector database, keep the `search(query, k, domain)` signature and swap the internals.
"""
import math
import re
from collections import Counter
from pathlib import Path

from . import domains

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
K1, B = 1.5, 0.75
_index = {}


def _tokens(text):
    out = []
    for t in re.findall(r"[a-z0-9]+", text.lower()):
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def build(domain):
    chunks = []
    for folder in ("common", domain):
        for f in sorted((KNOWLEDGE_DIR / folder).glob("*.md")):
            title = ""
            for para in re.split(r"\n\s*\n", f.read_text(encoding="utf-8")):
                para = para.strip()
                if para.startswith("#"):
                    title = para.lstrip("# ").strip()
                elif para:
                    tf = Counter(_tokens(title + " " + para))
                    chunks.append({"source": f.name, "title": title, "text": para, "tf": tf, "len": sum(tf.values())})
    n = max(len(chunks), 1)
    df = Counter(t for c in chunks for t in c["tf"])
    idf = {t: math.log((n - d + 0.5) / (d + 0.5) + 1) for t, d in df.items()}
    _index[domain] = (chunks, idf, sum(c["len"] for c in chunks) / n or 1)
    return _index[domain]


def search(query, k=3, domain=None):
    domain = domain or domains.DEFAULT
    chunks, idf, avg = _index.get(domain) or build(domain)
    terms = set(_tokens(query)) & idf.keys()
    scored = []
    for c in chunks:
        tf = c["tf"]
        score = sum(idf[t] * tf[t] * (K1 + 1) / (tf[t] + K1 * (1 - B + B * c["len"] / avg)) for t in terms if t in tf)
        if score > 0:
            scored.append({"source": c["source"], "title": c["title"], "text": c["text"], "score": round(score, 3)})
    return sorted(scored, key=lambda x: -x["score"])[:k]
