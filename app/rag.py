"""Retrieval layer over the knowledge/ folder (SOPs, supplier agreements, policies).

Uses a dependency-free TF-IDF index so the project runs anywhere. To move to a vector database,
keep the `search(query, k)` signature and swap the internals for embeddings.
"""
import math
import re
from collections import Counter
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
_index = None


def _tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def build():
    global _index
    chunks = []
    for f in sorted(KNOWLEDGE_DIR.glob("*.md")):
        title = ""
        for para in re.split(r"\n\s*\n", f.read_text(encoding="utf-8")):
            para = para.strip()
            if para.startswith("#"):
                title = para.lstrip("# ").strip()
            elif para:
                chunks.append({"source": f.name, "title": title, "text": para})
    df = Counter()
    for c in chunks:
        c["tf"] = Counter(_tokens(c["title"] + " " + c["text"]))
        df.update(c["tf"].keys())
    n = max(len(chunks), 1)
    idf = {t: math.log((n + 1) / (d + 1)) + 1 for t, d in df.items()}
    for c in chunks:
        c["vec"] = {t: v * idf[t] for t, v in c["tf"].items()}
        c["norm"] = math.sqrt(sum(v * v for v in c["vec"].values())) or 1
    _index = (chunks, idf)


def search(query, k=3):
    if _index is None:
        build()
    chunks, idf = _index
    q = Counter(_tokens(query))
    qv = {t: v * idf.get(t, 0) for t, v in q.items()}
    qn = math.sqrt(sum(v * v for v in qv.values())) or 1
    scored = []
    for c in chunks:
        score = sum(w * c["vec"].get(t, 0) for t, w in qv.items()) / (qn * c["norm"])
        if score > 0:
            scored.append({"source": c["source"], "title": c["title"], "text": c["text"], "score": round(score, 3)})
    return sorted(scored, key=lambda x: -x["score"])[:k]
