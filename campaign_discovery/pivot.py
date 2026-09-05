"""Automatic query generation and execution: the self-expanding loop.

    CASE -> EXTRACT -> PIVOT -> DISCOVER -> NEW CASE -> ...

`generate_queries` is pure (no I/O) so it is easy to test. `run_pending`
executes pending reddit/github queries through the collectors.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import db
from .extract.phrases import STOPWORDS

log = logging.getLogger("cdisc.pivot")

def _windows(phrase: str, lure_keywords: set[str], min_len: int = 4, max_len: int = 7,
             generic: set[str] = frozenset()) -> list[str]:
    """Slide over a normalised phrase and return keyword-bearing, non-generic windows."""
    words = phrase.split()
    out: list[str] = []
    for n in range(max_len, min_len - 1, -1):
        for i in range(0, len(words) - n + 1):
            w = words[i:i + n]
            if w[0] in STOPWORDS or w[-1] in STOPWORDS:
                continue
            if not any(t in lure_keywords for t in w):
                continue
            content = [t for t in w if t not in STOPWORDS]
            if len(content) < 3:
                continue
            cand = " ".join(w)
            if cand in generic:
                continue
            out.append(cand)
        if out:
            break  # longest useful windows only
    return out[:3]


# Sources whose indicators are *citations* (news articles link to registries,
# vendors, other coverage). Only their wording is worth pivoting on.
ARTICLE_SOURCES = {"rss", "github"}
# Domain suffixes that are never scam infrastructure worth searching for.
_NO_PIVOT_SUFFIX = (".gov", ".gov.au", ".gov.uk", ".edu", ".mil", ".int")

PRIORITY = {"telegram": 1, "phrase": 2, "email": 2, "wallet": 2, "domain": 3, "github_user": 3, "brand": 4}


def generate_queries(phrases: Iterable[str], observables: dict[str, list[str]],
                     lexicon: dict[str, Any], source_class: str | None = None) -> list[tuple[str, str, int]]:
    """Return [(kind, query, priority)] where kind in reddit | github | web.

    Priority 1 is run first (recruiter handles), 4 last (brand + "scam")."""
    kws = {k.lower() for k in lexicon.get("lure_keywords", [])}
    generic = {g.lower() for g in lexicon.get("generic_phrases", [])}
    tpl = lexicon.get("pivot_templates") or {}
    queries: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    article = (source_class or "") in ARTICLE_SOURCES

    def add(kind: str, q: str, prio: int) -> None:
        key = f"{kind}|{q.lower()}"
        if key not in seen and len(q) <= 200:
            seen.add(key)
            queries.append((kind, q, prio))

    distinctive: list[str] = []
    for p in list(phrases)[:3]:
        for w in _windows(p, kws, generic=generic):
            if w not in distinctive:
                distinctive.append(w)
    for phrase in distinctive[:2]:
        add("reddit", f'"{phrase}"', PRIORITY["phrase"])
        add("github", f'"{phrase}"', PRIORITY["phrase"])
        for t in tpl.get("phrase_web") or ['"{phrase}"']:
            add("web", t.format(phrase=phrase), PRIORITY["phrase"])

    for handle in observables.get("telegram", [])[:3]:
        add("reddit", f"@{handle}", PRIORITY["telegram"])
        for t in tpl.get("handle_web") or ['"@{handle}"']:
            add("web", t.format(handle=handle), PRIORITY["telegram"])
    for w in observables.get("wallet_eth", [])[:2] + observables.get("wallet_btc", [])[:2] \
            + observables.get("wallet_trx", [])[:2]:
        add("web", w, PRIORITY["wallet"])

    if article:
        return queries  # citations are not leads

    for dom in [d for d in observables.get("domain", []) if not d.endswith(_NO_PIVOT_SUFFIX)][:2]:
        add("reddit", dom, PRIORITY["domain"])
        add("github", f'"{dom}"', PRIORITY["domain"])
        add("web", f'"{dom}"', PRIORITY["domain"])
    for email in observables.get("email", [])[:1]:
        add("reddit", email, PRIORITY["email"])
        add("web", f'"{email}"', PRIORITY["email"])
    for user in observables.get("github_user", [])[:2]:
        add("web", f'"github.com/{user}"', PRIORITY["github_user"])
        add("reddit", f"github.com/{user}", PRIORITY["github_user"])
    for brand in observables.get("brand", [])[:2]:
        for t in tpl.get("brand_reddit") or ["{brand} scam"]:
            add("reddit", t.format(brand=brand), PRIORITY["brand"])
        for t in tpl.get("brand_web") or ['"{brand}" scam']:
            add("web", t.format(brand=brand), PRIORITY["brand"])
    return queries


def store_queries(conn: sqlite3.Connection, case_id: str, generation: int,
                  queries: Iterable[tuple[str, str, int]]) -> int:
    n = 0
    for kind, term, prio in queries:
        cur = conn.execute(
            "INSERT OR IGNORE INTO search_terms(id, term, kind, origin_case, generation, priority, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (db.next_id(conn, "Q", 6), term, kind, case_id, generation, prio, db.now_iso(),
             "pending" if kind in ("reddit", "github") else "manual"))
        n += cur.rowcount
    return n


def regenerate(conn: sqlite3.Connection, lexicon: dict[str, Any]) -> int:
    """Drop unexecuted queries and rebuild them from every case (after rule changes)."""
    conn.execute("DELETE FROM search_terms WHERE status IN ('pending', 'manual')")
    total = 0
    for case in conn.execute("SELECT c.id, c.source, c.lure_text, r.generation FROM cases c "
                             "JOIN raw_items r ON r.id = c.raw_item_id").fetchall():
        obs: dict[str, list[str]] = {}
        for o in conn.execute("SELECT o.type, o.value FROM case_observables co JOIN observables o "
                              "ON o.id = co.observable_id WHERE co.case_id = ? AND co.role != 'links-to' "
                              "OR (co.case_id = ? AND o.type IN ('domain','url'))", (case["id"], case["id"])):
            obs.setdefault(o["type"], []).append(o["value"])
        phrases = [l for l in (case["lure_text"] or "").split("\n") if l]
        total += store_queries(conn, case["id"], int(case["generation"] or 0) + 1,
                               generate_queries(phrases, obs, lexicon, case["source"].split(":")[0]))
    conn.commit()
    return total


def run_pending(conn: sqlite3.Connection, sources_cfg: dict[str, Any], limit: int | None = None,
                lookback_days: int | None = None) -> dict[str, int]:
    """Execute pending reddit/github search terms; insert new raw items."""
    from .collectors.github import GitHubCollector
    from .collectors.reddit_arctic import RedditArcticCollector

    pcfg = sources_cfg.get("pivot", {})
    max_gen = int(pcfg.get("max_generation", 2))
    limit = limit or int(pcfg.get("max_queries_per_run", 25))
    subs = pcfg.get("reddit_subreddits") or None
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days or int(pcfg.get("lookback_days", 180)))

    rows = conn.execute(
        "SELECT * FROM search_terms WHERE status = 'pending' AND generation <= ? "
        "ORDER BY priority, generation, created_at LIMIT ?", (max_gen, limit)).fetchall()
    reddit = RedditArcticCollector(sources_cfg.get("reddit", {}))
    github = GitHubCollector(sources_cfg.get("github", {}))
    stats = {"queries": 0, "hits": 0, "new_items": 0}
    for row in rows:
        stats["queries"] += 1
        hits = new = 0
        try:
            if row["kind"] == "reddit":
                items = reddit.search(row["term"], since=since, subreddits=subs, generation=row["generation"])
            elif row["kind"] == "github":
                items = github.search(row["term"], since=since, generation=row["generation"])
            else:
                continue
            for item in items:
                hits += 1
                if db.insert_raw(conn, item.to_row()):
                    new += 1
            status = "done"
        except Exception as exc:  # keep the loop alive
            log.warning("pivot %s %r: %s", row["kind"], row["term"], exc)
            status = "error"
        conn.execute("UPDATE search_terms SET last_run=?, hits=?, new_items=?, status=? WHERE id=?",
                     (db.now_iso(), hits, new, status, row["id"]))
        conn.commit()
        stats["hits"] += hits
        stats["new_items"] += new
        log.info("pivot %-6s %-60s hits=%d new=%d", row["kind"], row["term"][:60], hits, new)
    return stats


def pivots_for_cluster(summary: dict[str, Any], labels: dict[str, str] | None = None) -> list[str]:
    """Human-readable recommended pivots for a cluster summary."""
    labels = labels or {}
    out: list[str] = []
    for h in summary.get("shared_telegram", [])[:2]:
        out.append(f"{labels.get('contact_pivot', 'Search contact handle')} @{h} across Reddit / GitHub / web")
    if summary.get("shared_domains"):
        out.append("Resolve sibling domains (shared IP / nameserver / registrar) for: "
                   + ", ".join(summary["shared_domains"][:3]))
    if summary.get("common_lure"):
        out.append(f'Search phrase across archive: "{summary["common_lure"][:80]}"')
    if summary.get("repos"):
        out.append("Inspect GitHub account history and commit metadata of "
                   + ", ".join(summary["repos"][:3]))
    if summary.get("shared_dependencies"):
        out.append("Check shared dependencies on npm/PyPI for malicious versions: "
                   + ", ".join(summary["shared_dependencies"][:3]))
    if summary.get("brands"):
        out.append("Check impersonated brands' abuse contacts / official channels: "
                   + ", ".join(summary["brands"][:3]))
    if summary.get("young_domains"):
        out.append("Newly registered domains worth a URL scan: " + ", ".join(summary["young_domains"][:3]))
    if not out:
        out.append("Read the cases side by side; wording-only cluster")
    return out
