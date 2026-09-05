"""Relevance scoring (0..1) from weighted lexicon hits.

A post must show *both* the profile's topic context (e.g. employment) and a
harm context (fraud / malware) to be relevant, unless it names a known campaign.
The score is deliberately simple and transparent so it can be tuned in the
profile's lexicon.yaml without touching code.
"""
from __future__ import annotations

import math
import re
from typing import Any


def _hits(text_l: str, terms: dict[str, float]) -> tuple[float, list[str]]:
    total = 0.0
    matched = []
    for term, w in terms.items():
        t = term.lower()
        pat = rf"\b{re.escape(t)}" if len(t) > 3 else rf"\b{re.escape(t)}\b"
        if re.search(pat, text_l):
            total += float(w)
            matched.append(term)
    return total, matched


def _norm(text: str) -> str:
    tl = (text or "").lower()
    if "-" in tl:  # "contagious-interview" tag == "contagious interview"
        tl = tl + " " + tl.replace("-", " ")
    return tl


def relevance_score(text: str, lexicon: dict[str, Any], context: dict[str, float] | None = None,
                    title: str | None = None) -> tuple[float, dict[str, Any]]:
    """`context` adds implicit weight for a source whose venue already implies a
    class ({"harm": 1.5} for r/Scams: every post there is about fraud even when
    it never says "scam"). Terms found in `title` count twice: "Is this job a
    scam?" is decisive even when the body is a screenshot caption."""
    tl = _norm(text)
    topic, topic_m = _hits(tl, lexicon.get("topic_terms", {}))
    harm, harm_m = _hits(tl, lexicon.get("harm_terms", {}))
    camp, camp_m = _hits(tl, lexicon.get("campaign_terms", {}))
    if title:
        t = _norm(title)
        topic += _hits(t, lexicon.get("topic_terms", {}))[0]
        harm += _hits(t, lexicon.get("harm_terms", {}))[0]
        camp += _hits(t, lexicon.get("campaign_terms", {}))[0]
    for cls, w in (context or {}).items():
        if cls == "topic":
            topic += float(w); topic_m.append("<venue>")
        elif cls == "harm":
            harm += float(w); harm_m.append("<venue>")
        elif cls == "campaign":
            camp += float(w); camp_m.append("<venue>")

    # saturating transforms so one very long post does not dominate
    t_ = 1 - math.exp(-topic / 2.0)
    h_ = 1 - math.exp(-harm / 2.0)
    c_ = 1 - math.exp(-camp / 1.5)

    # both contexts required; campaign names can carry a post alone
    score = math.sqrt(t_ * h_)
    # One stray harm word in an on-topic forum ("...this market is a scam") is
    # not evidence. Require ~1.5 of harm-term weight (a harm word in the title,
    # two different terms, or a fraud venue plus one term); dampen below that.
    if harm < 1.5 and camp == 0:
        score *= harm / 1.5
    score = max(score, c_ * 0.9)
    return round(min(score, 1.0), 3), {"topic": topic_m, "harm": harm_m, "campaign": camp_m}
