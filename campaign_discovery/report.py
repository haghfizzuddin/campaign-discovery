"""Render candidate clusters as analyst-facing triage notes (text, markdown, JSON).

Wording that belongs to the theme (what a brand is called, what the lure is)
comes from the active profile's `labels`."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db
from .extract.defang import defang


ORDER = {"strongly_clustered": 0, "probable": 1, "possible": 2}


def _labels() -> dict[str, str]:
    from . import config
    try:
        return config.lexicon().get("labels") or {}
    except FileNotFoundError:
        return {}


def _clusters(conn: sqlite3.Connection, new_only: bool, min_confidence: str | None, limit: int | None) -> list[sqlite3.Row]:
    order = ORDER
    q = "SELECT * FROM clusters WHERE status != 'closed'"
    if new_only:
        q += " AND is_new = 1"
    rows = conn.execute(q).fetchall()
    if min_confidence:
        rows = [r for r in rows if order.get(r["confidence"], 9) <= order[min_confidence.lower()]]
    rows.sort(key=lambda r: (order.get(r["confidence"], 9), -r["size"], r["updated_at"]))
    return rows[:limit] if limit else rows


def _fmt_list(items: list[str], indent: str = "  ", defang_urls: bool = False, empty: str = "  (none)") -> str:
    if not items:
        return empty
    return "\n".join(f"{indent}- {defang(i) if defang_urls else i}" for i in items)


def render_cluster_text(conn: sqlite3.Connection, row: sqlite3.Row, labels: dict[str, str] | None = None) -> str:
    s: dict[str, Any] = json.loads(row["summary"] or "{}")
    labels = labels if labels is not None else _labels()
    cases = conn.execute(
        "SELECT c.id, c.source, c.url, c.title, c.published_at FROM cluster_cases cc "
        "JOIN cases c ON c.id = cc.case_id WHERE cc.cluster_id = ? ORDER BY c.published_at", (row["id"],)).fetchall()
    header = "NEW CANDIDATE CLUSTER" if row["is_new"] else "CANDIDATE CLUSTER"
    lines = [
        f"{header}  {row['id']}  [{row['confidence']}]",
        f"Label: {row['label']}",
        "",
        f"Cases: {row['size']}",
        f"First seen: {(s.get('first_seen') or '?')[:10]}",
        f"Sources: {', '.join(f'{k} ({v})' for k, v in (s.get('sources') or {}).items())}",
        "",
        f"{labels.get('lure', 'Common lure')}:",
        f'  "{s.get("common_lure") or "-"}"',
        "",
        "Lure type(s): " + (", ".join(f"{k} ({v})" for k, v in (s.get("lure_types") or {}).items()) or "-"),
        "",
        f"{labels.get('brands', 'Brands impersonated')}:",
        _fmt_list(s.get("brands", [])),
        "",
        "Shared infrastructure:",
        _fmt_list(s.get("shared_domains", []) + s.get("shared_urls", []) + s.get("shared_ips", []),
                  defang_urls=True),
    ]
    if s.get("shared_telegram") or s.get("shared_emails"):
        lines += ["", "Shared contact:",
                  _fmt_list([f"@{h}" for h in s.get("shared_telegram", [])] + s.get("shared_emails", []))]
    if s.get("shared_wallets"):
        lines += ["", "Shared wallets:", _fmt_list(s["shared_wallets"])]
    lines += ["", f"Related repositories: {len(s.get('repos', []))}"]
    if s.get("repos"):
        lines.append(_fmt_list([f"github.com/{r}" for r in s["repos"][:6]]))
    if s.get("shared_dependencies"):
        lines += ["", "Shared dependencies:", _fmt_list(s["shared_dependencies"])]
    if s.get("young_domains"):
        lines += ["", "Newly registered domains (<= 90 days):", _fmt_list(s["young_domains"], defang_urls=True)]
    lines += ["", f"Confidence: {row['confidence']}  ({s.get('confidence_basis', '')})", "", "Evidence:"]
    for e in s.get("evidence", [])[:12]:
        v = defang(e["value"]) if e["feature"] in ("domain", "url") else e["value"]
        lines.append(f"  [{e['specificity']:<6}] {e['feature']:<19} {v[:60]:<60} {', '.join(e['cases'][:4])}"
                     + (f" +{len(e['cases']) - 4}" if len(e["cases"]) > 4 else ""))
    if not s.get("evidence"):
        lines.append("  (none recorded)")
    assess = json.loads(row["analyst_assessment"]) if row["analyst_assessment"] else {}
    lines += ["", f"Analyst assessment: same_operational_lineage = {assess.get('lineage', 'unknown')}"
              + (f"  ({assess.get('note')})" if assess.get("note") else "")]
    lines += ["", "Why this cluster exists:",
              _fmt_list(s.get("reasons", []), empty="  (no explicit reasons recorded)"),
              "", "Recommended pivots:",
              "\n".join(f"  -> {p}" for p in s.get("pivots", [])) or "  (none)",
              "", "Cases:"]
    for c in cases:
        lines.append(f"  {c['id']}  {(c['published_at'] or '')[:10]:10}  {c['source']:<18} {(c['title'] or '')[:70]}")
        if c["url"]:
            lines.append(f"      {c['url']}")
    if row["llm_notes"]:
        lines += ["", "Analyst model notes:", row["llm_notes"]]
    return "\n".join(lines)


def set_assessment(conn: sqlite3.Connection, cluster_id: str, lineage: str, note: str | None) -> dict[str, Any]:
    """Record the analyst's call on operator lineage. Never derived by the engine."""
    if lineage not in ("same", "different", "unknown"):
        raise ValueError("lineage must be same | different | unknown")
    if not conn.execute("SELECT 1 FROM clusters WHERE id = ?", (cluster_id,)).fetchone():
        raise ValueError(f"unknown cluster {cluster_id}")
    rec = {"lineage": lineage, "note": note, "assessed_at": db.now_iso()}
    conn.execute("UPDATE clusters SET analyst_assessment = ?, status = CASE WHEN status IN ('new','seen') "
                 "THEN 'investigating' ELSE status END WHERE id = ?", (json.dumps(rec), cluster_id))
    conn.commit()
    return rec


def leads(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Single-case signals strong enough to look at even without a cluster:
    newly registered domains, Telegram handles, young GitHub repos/accounts, wallets."""
    out: list[dict[str, Any]] = []
    clustered = {r["case_id"] for r in conn.execute(
        "SELECT cc.case_id FROM cluster_cases cc JOIN clusters c ON c.id = cc.cluster_id WHERE c.status != 'closed'")}
    rows = conn.execute(
        "SELECT o.id, o.type, o.value, co.case_id, c.title, c.url, c.published_at, e.result "
        "FROM case_observables co JOIN observables o ON o.id = co.observable_id "
        "JOIN cases c ON c.id = co.case_id "
        "LEFT JOIN enrichment e ON e.observable_id = o.id AND e.provider IN ('rdap', 'github') "
        "WHERE c.status != 'false_positive' AND o.type IN "
        "('domain', 'telegram', 'github_repo', 'github_user', 'wallet_btc', 'wallet_eth', 'wallet_trx')").fetchall()
    seen: set[str] = set()
    for r in rows:
        if r["case_id"] in clustered or r["id"] in seen:
            continue
        info = json.loads(r["result"]) if r["result"] else {}
        why = None
        if r["type"] == "domain" and info.get("newly_registered"):
            why = f"registered {info.get('age_days')}d ago via {info.get('registrar') or '?'}"
        elif r["type"] == "telegram":
            why = "Telegram contact handle"
        elif r["type"] == "github_repo" and info.get("young_repo"):
            why = f"repo {info.get('repo_age_days')}d old, {info.get('stars', 0)} stars"
        elif r["type"] == "github_user" and info.get("young_account"):
            why = f"account {info.get('account_age_days')}d old"
        elif r["type"].startswith("wallet_"):
            why = "crypto wallet in a report"
        if why:
            seen.add(r["id"])
            out.append({"type": r["type"], "value": r["value"], "why": why, "case": r["case_id"],
                        "title": r["title"], "url": r["url"], "published_at": r["published_at"]})
    rank = {"domain": 0, "telegram": 1, "github_repo": 2, "github_user": 3}
    out.sort(key=lambda x: (rank.get(x["type"], 9), x["published_at"] or ""), reverse=False)
    return out[:limit]


def render_leads_text(conn: sqlite3.Connection) -> str:
    items = leads(conn)
    if not items:
        return ""
    lines = ["LEADS OUTSIDE CLUSTERS  (single cases with a strong signal)", ""]
    for it in items:
        v = defang(it["value"]) if it["type"] == "domain" else it["value"]
        lines.append(f"  {it['type']:<12} {v:<42} {it['why']}")
        lines.append(f"      {it['case']}  {(it['published_at'] or '')[:10]}  {(it['title'] or '')[:60]}")
    return "\n".join(lines)


def render(conn: sqlite3.Connection, fmt: str = "text", new_only: bool = False,
           min_confidence: str | None = None, limit: int | None = None) -> str:
    rows = _clusters(conn, new_only, min_confidence, limit)
    if fmt == "json":
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = json.loads(d["summary"] or "{}")
            out.append(d)
        return json.dumps(out, indent=1, ensure_ascii=False)
    labels = _labels()
    blocks = [render_cluster_text(conn, r, labels) for r in rows]
    if not blocks:
        blocks = ["No candidate clusters" + (" marked new." if new_only else ".")]
    lead_block = render_leads_text(conn)
    if lead_block:
        blocks.append(lead_block)
    if fmt == "md":
        st = db.stats(conn)
        head = (f"# campaign-discovery report\n\nGenerated {db.now_iso()} · {st['cases']} cases · "
                f"{st['observables']} observables · {len(rows)} cluster(s) shown\n")
        return head + "\n\n".join(f"```text\n{b}\n```" for b in blocks) + "\n"
    sep = "\n" + "=" * 78 + "\n"
    return sep.join(blocks) + "\n"


def mark_seen(conn: sqlite3.Connection) -> int:
    cur = conn.execute("UPDATE clusters SET is_new = 0, status = CASE WHEN status='new' THEN 'seen' ELSE status END "
                       "WHERE is_new = 1")
    conn.commit()
    return cur.rowcount
