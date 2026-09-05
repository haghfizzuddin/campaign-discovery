"""Text similarity for lure wording.

Default: word-shingle MinHash (no dependencies). Optional: sentence-transformers
embeddings when installed and enabled in sources.yaml (clustering.use_embeddings).
"""
from __future__ import annotations

import hashlib
import logging
import struct
from typing import Iterable, Sequence

from ..extract.phrases import STOPWORDS, normalise

log = logging.getLogger("cdisc.cluster.sim")

N_PERM = 96
_MERSENNE = (1 << 61) - 1
_MAX = (1 << 32) - 1


def _seeds(n: int = N_PERM) -> list[tuple[int, int]]:
    out = []
    for i in range(n):
        h = hashlib.blake2b(f"cdisc-minhash-{i}".encode(), digest_size=16).digest()
        a, b = struct.unpack("<QQ", h)
        out.append((a % _MERSENNE or 1, b % _MERSENNE))
    return out


_SEEDS = _seeds()


def shingles(text: str, k: int = 2) -> set[str]:
    """Word bigrams over stopword-stripped text. Bigrams tolerate light
    paraphrasing ("schedule" vs "scheduling") better than trigrams while still
    demanding shared word order, which is what copied scam scripts exhibit."""
    words = [w for w in normalise(text).split() if w not in STOPWORDS]
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def _h32(s: str) -> int:
    return struct.unpack("<I", hashlib.blake2b(s.encode(), digest_size=4).digest())[0]


def minhash(sh: Iterable[str]) -> list[int]:
    hs = [_h32(s) for s in sh]
    if not hs:
        return [_MAX] * N_PERM
    sig = []
    for a, b in _SEEDS:
        sig.append(min(((a * h + b) % _MERSENNE) & _MAX for h in hs))
    return sig


def estimate_jaccard(a: Sequence[int], b: Sequence[int]) -> float:
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similar_pairs(texts: dict[str, str], threshold: float, min_shingles: int = 6) -> list[tuple[str, str, float]]:
    """Return (id_a, id_b, score) for pairs whose lure text is similar.

    Uses MinHash to shortlist, then exact Jaccard on shingles to confirm, so
    the reported score is exact. Texts with fewer than `min_shingles` bigrams
    are skipped: two five-word titles sharing "job scam" is not evidence.
    O(n^2) signature compares; fine to a few thousand cases (a LSH banding
    step is the natural upgrade after that)."""
    ids = list(texts)
    sh = {i: shingles(texts[i]) for i in ids}
    sig = {i: minhash(sh[i]) for i in ids if len(sh[i]) >= min_shingles}
    out: list[tuple[str, str, float]] = []
    keep = [i for i in ids if i in sig]
    for x in range(len(keep)):
        for y in range(x + 1, len(keep)):
            a, b = keep[x], keep[y]
            if estimate_jaccard(sig[a], sig[b]) < threshold * 0.7:
                continue
            j = jaccard(sh[a], sh[b])
            if j >= threshold:
                out.append((a, b, round(j, 3)))
    return out


def embedding_pairs(texts: dict[str, str], threshold: float) -> list[tuple[str, str, float]]:
    """Cosine similarity with sentence-transformers (optional dependency)."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        log.info("sentence-transformers not installed; skipping embedding similarity")
        return []
    ids = [i for i in texts if texts[i].strip()]
    if len(ids) < 2:
        return []
    model = SentenceTransformer("all-MiniLM-L6-v2")
    vecs = model.encode([texts[i] for i in ids], normalize_embeddings=True)
    out: list[tuple[str, str, float]] = []
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            s = float(vecs[x] @ vecs[y])
            if s >= threshold:
                out.append((ids[x], ids[y], round(s, 3)))
    return out
