"""Lure phrase mining, company detection, lure-type and stage classification.

Everything here is deterministic and lexicon-driven. The LLM layer (optional)
refines these outputs; it never replaces them.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}|\r?\n(?=[A-Z\"'“])")
_QUOTE_RE = re.compile(r"[\"“]([^\"”]{20,240})[\"”]")
_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s'$@#.-]")
_URLISH = re.compile(r"https?://\S+|\S+\.\S+/\S*")

STOPWORDS = set("""
a an the and or but if then so of to in on at for from by with about as into like through after over
between out against during without before under around among is are was were be been being have has had
do does did will would shall should can could may might must i you he she it we they me him her us them
my your his its our their this that these those there here what which who whom whose when where why how
all any both each few more most other some such no nor not only own same than too very just also
""".split())


def normalise(s: str) -> str:
    s = _URLISH.sub(" ", s)
    s = s.lower().replace("’", "'")
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip(" .-")


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text or "") if p and p.strip()]
    return [p for p in parts if 3 <= len(p.split()) <= 60]


def extract_phrases(text: str, lure_keywords: Iterable[str], max_phrases: int = 12) -> list[str]:
    """Return candidate lure sentences: quoted strings first, then sentences
    containing lure keywords, ranked by keyword density."""
    kws = [k.lower() for k in lure_keywords]
    seen: set[str] = set()
    scored: list[tuple[float, str]] = []

    for m in _QUOTE_RE.finditer(text or ""):
        q = normalise(m.group(1))
        if 4 <= len(q.split()) <= 40 and q not in seen:
            seen.add(q)
            scored.append((10.0 + sum(k in q for k in kws), q))

    for s in sentences(text or ""):
        n = normalise(s)
        if not n or n in seen or len(n.split()) < 5:
            continue
        hits = sum(1 for k in kws if re.search(rf"\b{re.escape(k)}\b", n))
        if hits == 0:
            continue
        # density: reward short, keyword-dense sentences (they read like scripts)
        density = hits / max(5, len(n.split())) * 20
        seen.add(n)
        scored.append((hits + density, n))

    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:max_phrases]]


def _score_keywords(text_l: str, keywords: dict[str, float]) -> float:
    total = 0.0
    for kw, w in keywords.items():
        if kw in text_l:
            total += float(w)
    return total


def classify_lure(text: str, lure_types: dict[str, Any]) -> tuple[str | None, dict[str, float]]:
    tl = (text or "").lower()
    if "-" in tl:
        tl = tl + " " + tl.replace("-", " ")
    scores: dict[str, float] = {}
    for name, spec in lure_types.items():
        s = _score_keywords(tl, spec.get("keywords", {}))
        if s >= float(spec.get("min_score", 1.5)):
            scores[name] = round(s, 2)
    if not scores:
        return None, {}
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return best, dict(sorted(scores.items(), key=lambda kv: -kv[1]))


def classify_stage(text: str, stages: dict[str, list[str]]) -> list[str]:
    """Return all funnel stages the report touches, in funnel order."""
    tl = (text or "").lower()
    hit = []
    for stage, cues in stages.items():
        if any(c.lower() in tl for c in cues):
            hit.append(stage)
    return hit


_DEFAULT_BRAND_CUE = r"impersonat|posing|pretend|claim|fake|scam|from|at"


def extract_brands(
    text: str, brands: Iterable[str], cue_patterns: Iterable[str] | None = None,
    exclusions: Iterable[str] | None = None, context_regex: str | None = None, window: int = 120,
) -> dict[str, float]:
    """Return {brand: confidence}.

    Lexicon brands must appear with their canonical capitalisation ("Target",
    not "target") and within `window` chars of a profile cue (`context_regex`,
    e.g. recruiter|hiring|interview); they score 0.9. Cue-pattern captures
    ("recruiter from Zorbtech") score 0.5. `exclusions` are phrases blanked out
    first ("Google Meet", "Zoom call")."""
    out: dict[str, float] = {}
    t = text or ""
    try:
        cue = re.compile(context_regex or _DEFAULT_BRAND_CUE, re.I)
    except re.error:
        cue = re.compile(_DEFAULT_BRAND_CUE, re.I)
    for ex in exclusions or []:
        t = re.sub(re.escape(ex), " " * len(ex), t, flags=re.I)
    for c in brands:
        pat = re.compile(rf"(?<![\w-]){re.escape(c)}(?![\w-])")
        for m in pat.finditer(t):
            around = t[max(0, m.start() - window): m.end() + window]
            if cue.search(around):
                out[c] = max(out.get(c, 0), 0.9)
                break
    for p in cue_patterns or []:
        try:
            rx = re.compile(p)
        except re.error:
            continue
        for m in rx.finditer(t):
            name = m.group(1).strip(" .,;:")
            if len(name) < 3 or name.lower() in _CUE_NOISE:
                continue
            # prefer canonical casing from the lexicon if the capture matches one
            canon = next((c for c in brands if c.lower() == name.lower()), name)
            out[canon] = max(out.get(canon, 0), 0.9 if canon != name else 0.5)
    return out


_CUE_NOISE = {
    "the", "this", "that", "a", "an", "my", "our", "your", "their", "his", "her", "its",
    "linkedin", "indeed", "reddit", "google", "telegram", "whatsapp", "email", "gmail", "home",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "i", "me",
    "hr", "recruiter", "someone", "company", "startup", "remote", "us", "usa", "uk", "eu",
}


def top_phrases(phrase_lists: Iterable[Iterable[str]], n: int = 3) -> list[tuple[str, int]]:
    c: Counter[str] = Counter()
    for pl in phrase_lists:
        for p in set(pl):
            c[p] += 1
    return c.most_common(n)
