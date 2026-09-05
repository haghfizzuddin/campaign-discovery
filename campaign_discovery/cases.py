"""Case engine: turn raw items into cases, observables and graph edges."""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Iterable

from . import db
from .extract import (classify_lure, classify_stage, extract_brands, extract_iocs,
                      extract_phrases, relevance_score)
from .pivot import generate_queries, store_queries

log = logging.getLogger("cdisc.cases")

# observable type -> (edge role, graph predicate)
ROLE_FOR_TYPE = {
    "url": ("links-to", "links-to"),
    "domain": ("links-to", "links-to"),
    "ip": ("links-to", "resolves-to"),
    "email": ("contact", "contacted-via"),
    "telegram": ("contact", "uses-telegram"),
    "github_repo": ("delivers", "delivers"),
    "github_user": ("delivers", "operated-by"),
    "wallet_btc": ("pays-to", "pays-to"),
    "wallet_eth": ("pays-to", "pays-to"),
    "wallet_trx": ("pays-to", "pays-to"),
    "md5": ("hash", "has-hash"),
    "sha1": ("hash", "has-hash"),
    "sha256": ("hash", "has-hash"),
}

DEFAULT_TRUSTED_SOURCES = ("urlhaus", "manual")


def _snippet(text: str, needle: str, width: int = 80) -> str | None:
    i = text.lower().find(needle.lower())
    if i < 0:
        return None
    return " ".join(text[max(0, i - width): i + len(needle) + width].split())


_NOBODY = {"", "[deleted]", "automoderator", "unknown"}


def _norm_title(t: str | None) -> str:
    return " ".join((t or "").lower().split())


def _find_duplicate(conn: sqlite3.Connection, raw: sqlite3.Row) -> str | None:
    """Same author re-posting the same report (cross-post to another subreddit,
    repeat post) is one case, not a cluster of two."""
    author = (raw["author"] or "").lower()
    if author in _NOBODY:
        return None
    title = _norm_title(raw["title"])
    body = " ".join((raw["body"] or "").split())
    for c in conn.execute(
            "SELECT id, title, body FROM cases WHERE lower(author) = ? AND published_at BETWEEN "
            "datetime(?, '-7 days') AND datetime(?, '+7 days')",
            (author, raw["published_at"] or db.now_iso(), raw["published_at"] or db.now_iso())):
        if title and _norm_title(c["title"]) == title:
            return c["id"]
        cb = " ".join((c["body"] or "").split())
        if len(body) > 200 and body == cb:
            return c["id"]
    return None


def process_raw_item(conn: sqlite3.Connection, raw: sqlite3.Row, lexicon: dict[str, Any],
                     threshold: float, generate_pivots: bool = True,
                     trusted_sources: Iterable[str] = DEFAULT_TRUSTED_SOURCES) -> str | None:
    """Score a raw item; if relevant, create a case and all derived records.
    Returns the new case id or None."""
    text = f"{raw['title'] or ''}\n\n{raw['body'] or ''}".strip()
    source_class = raw["source"].split(":")[0]
    trusted = source_class in set(trusted_sources)
    ctx = lexicon.get("source_context", {})
    context = ctx.get(raw["source"]) or ctx.get(source_class)
    score, matched = relevance_score(text, lexicon, context, title=raw["title"])
    if trusted:
        score = max(score, 0.9)
    # broad security-news feeds need a higher bar than victim forums
    threshold = float((lexicon.get("threshold_by_source") or {}).get(source_class, threshold))

    if score < threshold:
        conn.execute("UPDATE raw_items SET processed = 1, relevance = ? WHERE id = ?", (score, raw["id"]))
        return None

    dup = _find_duplicate(conn, raw)
    if dup:
        conn.execute("UPDATE raw_items SET processed = 1, relevance = ?, case_id = ? WHERE id = ?",
                     (score, dup, raw["id"]))
        conn.execute("UPDATE cases SET notes = COALESCE(notes || char(10), '') || ? WHERE id = ?",
                     (f"Also posted: {raw['url'] or raw['source_ref']} ({raw['source']})", dup))
        db.add_relationship(conn, "raw_item", raw["id"], "duplicate-of", "case", dup, dup)
        log.info("%s <- duplicate %s %s", dup, raw["source"], (raw["title"] or "")[:60])
        return None

    case_id = db.next_id(conn, "CASE")
    phrases = extract_phrases(text, lexicon.get("lure_keywords", []))
    lure_type, _ = classify_lure(text, lexicon.get("lure_types", {}))
    stages = classify_stage(text, lexicon.get("stages", {}))
    lure_text = "\n".join(phrases)

    conn.execute(
        "INSERT INTO cases(id, raw_item_id, source, url, author, title, body, published_at, created_at, "
        "relevance, lure_type, stage, lure_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, raw["id"], raw["source"], raw["url"], raw["author"], raw["title"], raw["body"],
         raw["published_at"], db.now_iso(), score, lure_type, ",".join(stages), lure_text),
    )
    conn.execute("INSERT INTO cases_fts(case_id, title, body, lure_text) VALUES (?,?,?,?)",
                 (case_id, raw["title"] or "", raw["body"] or "", lure_text))
    conn.execute("UPDATE raw_items SET processed = 1, relevance = ?, case_id = ? WHERE id = ?",
                 (score, case_id, raw["id"]))

    # ---- indicators
    ignore = set(lexicon.get("ignore_domains", []))
    if raw["url"]:  # a blog's own images/links are never indicators
        from urllib.parse import urlsplit as _us
        own = (_us(raw["url"]).hostname or "").lower()
        if own:
            ignore.add(own)
            ignore.add(".".join(own.split(".")[-2:]))
    # Titles derived from the body (Mastodon, manual text) are cut at 140 chars and
    # can end mid-URL; extract from the body alone in that case.
    title, body = (raw["title"] or "").strip(), (raw["body"] or "").strip()
    ioc_text = body if title and body and body.startswith(title[:50]) else text
    iocs = extract_iocs(ioc_text, ignore)
    obs_by_type: dict[str, list[str]] = {}
    for otype, values in iocs.items():
        role, predicate = ROLE_FOR_TYPE.get(otype, ("mentions", "mentions"))
        for v in sorted(values):
            oid = db.upsert_observable(conn, otype, v)
            db.link_case_observable(conn, case_id, oid, role, 1.0, _snippet(text, v))
            db.add_relationship(conn, "case", case_id, predicate, otype, oid, case_id)
            obs_by_type.setdefault(otype, []).append(v)

    # url -> domain edges
    for u in iocs.get("url", []):
        from urllib.parse import urlsplit
        host = (urlsplit(u).hostname or "").lower()
        if host in iocs.get("domain", set()):
            u_id = conn.execute("SELECT id FROM observables WHERE type='url' AND value=?", (u,)).fetchone()["id"]
            d_id = conn.execute("SELECT id FROM observables WHERE type='domain' AND value=?", (host,)).fetchone()["id"]
            db.add_relationship(conn, "url", u_id, "hosted-on", "domain", d_id, case_id)

    # ---- brands impersonated
    brands = extract_brands(text, lexicon.get("brands", []), lexicon.get("brand_cue_patterns", []),
                            lexicon.get("brand_exclusions", []), lexicon.get("brand_context_regex"))
    for name, conf in brands.items():
        oid = db.upsert_observable(conn, "brand", name)
        db.link_case_observable(conn, case_id, oid, "impersonates", conf, _snippet(text, name))
        db.add_relationship(conn, "case", case_id, "impersonates", "brand", oid, case_id, conf)
        obs_by_type.setdefault("brand", []).append(name)

    # ---- lure phrases as observables (exact-phrase linking across posts).
    # Only script-like sentences qualify: >= 6 words and >= 2 lure keywords, so
    # generic "any advice appreciated" lines do not glue unrelated cases together.
    kws = [k.lower() for k in lexicon.get("lure_keywords", [])]
    for p in phrases[:6]:
        words = p.split()
        if len(words) >= 6 and sum(1 for k in kws if f" {k} " in f" {p} ") >= 2:
            oid = db.upsert_observable(conn, "phrase", p)
            db.link_case_observable(conn, case_id, oid, "phrase", 0.8)

    # ---- matched vocabulary for explainability
    if lure_type:
        oid = db.upsert_observable(conn, "lure_type", lure_type)
        db.link_case_observable(conn, case_id, oid, "classified", 0.7)

    # ---- automatic query generation
    if generate_pivots:
        store_queries(conn, case_id, int(raw["generation"] or 0) + 1,
                      generate_queries(phrases, obs_by_type, lexicon, source_class))

    log.info("%s <- %s (%.2f) %s", case_id, raw["source"], score, (raw["title"] or "")[:70])
    return case_id


def process_pending(conn: sqlite3.Connection, lexicon: dict[str, Any], threshold: float,
                    limit: int | None = None,
                    trusted_sources: Iterable[str] = DEFAULT_TRUSTED_SOURCES) -> tuple[int, int]:
    rows = db.unprocessed_raw(conn, limit)
    created = 0
    for raw in rows:
        with db.tx(conn):
            if process_raw_item(conn, raw, lexicon, threshold, trusted_sources=trusted_sources):
                created += 1
    return len(rows), created


def case_bundle(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not case:
        return None
    obs = conn.execute(
        "SELECT o.id, o.type, o.value, o.case_count, co.role, co.confidence, co.context "
        "FROM case_observables co JOIN observables o ON o.id = co.observable_id "
        "WHERE co.case_id = ? ORDER BY o.type, o.value", (case_id,)).fetchall()
    clusters = [r["cluster_id"] for r in conn.execute(
        "SELECT cluster_id FROM cluster_cases WHERE case_id = ?", (case_id,))]
    terms = conn.execute("SELECT term, kind, status, hits FROM search_terms WHERE origin_case = ?",
                         (case_id,)).fetchall()
    return {
        "case": dict(case),
        "observables": [dict(r) for r in obs],
        "clusters": clusters,
        "search_terms": [dict(r) for r in terms],
    }
