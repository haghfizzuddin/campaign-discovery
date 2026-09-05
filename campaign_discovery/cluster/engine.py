"""Cluster engine.

Cases are nodes in a NetworkX graph, linked by shared observables and by
similar lure wording. Connected components become candidate clusters. Each
cluster carries a structured evidence list (feature, value, specificity,
cases, independence group) and a confidence level on the ladder
possible -> probable -> strongly_clustered, derived only from how many
*independent high-specificity* features stack. Operator identity is never
derived here; see `analyst_assessment` on the clusters table.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from typing import Any

import networkx as nx

from .. import db
from ..pivot import pivots_for_cluster
from .similarity import embedding_pairs, similar_pairs

log = logging.getLogger("cdisc.cluster")

HARD_TYPES = {"domain", "url", "email", "telegram", "github_repo", "github_user",
              "wallet_btc", "wallet_eth", "wallet_trx", "md5", "sha1", "sha256"}
WEAK_DEFAULT = {"ip", "nameserver", "dependency"}
NEVER_LINK = {"brand", "lure_type", "malware_family"}  # descriptive, not infrastructure

LADDER = ["possible", "probable", "strongly_clustered"]
DEFAULT_SPECIFICITY = {
    "high": ["telegram", "email", "wallet_btc", "wallet_eth", "wallet_trx", "sha256", "sha1", "md5", "github_repo", "url"],
    "medium": ["domain", "github_user", "phrase", "wording_similarity"],
    "low": ["ip", "nameserver", "dependency", "registrar"],
}
# derived-from-domain predicates: these observables are not independent of their domain
_DERIVED = {"resolves-to", "uses-ns", "registered-via", "hosted-on"}

HUMAN_TYPE = {
    "domain": "domain", "url": "URL", "email": "email address", "telegram": "Telegram handle",
    "github_repo": "GitHub repository", "github_user": "GitHub account", "wallet_btc": "BTC wallet",
    "wallet_eth": "ETH wallet", "wallet_trx": "TRON wallet", "md5": "hash", "sha1": "hash",
    "sha256": "hash", "ip": "IP address", "nameserver": "nameserver", "dependency": "dependency",
    "phrase": "lure phrase",
}


def _load(conn: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, set[str]]]:
    cases = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, title, url, source, author, published_at, lure_type, lure_text FROM cases "
        "WHERE status != 'false_positive'")}
    obs: dict[str, dict[str, Any]] = {}
    obs_cases: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute(
            "SELECT o.id, o.type, o.value, co.case_id FROM case_observables co "
            "JOIN observables o ON o.id = co.observable_id"):
        if r["case_id"] not in cases:
            continue
        obs[r["id"]] = {"type": r["type"], "value": r["value"]}
        obs_cases[r["id"]].add(r["case_id"])
    return cases, obs, obs_cases


def build_graph(conn: sqlite3.Connection, ccfg: dict[str, Any]) -> tuple[nx.Graph, dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, set[str]]]:
    cases, obs, obs_cases = _load(conn)
    weak = set(ccfg.get("weak_types", list(WEAK_DEFAULT)))
    hub_cap = max(int(ccfg.get("hub_absolute", 25)),
                  int(float(ccfg.get("hub_fraction", 0.3)) * max(len(cases), 1)))

    G = nx.Graph()
    G.add_nodes_from(cases)
    G.graph["hubs"] = set()

    # ---- shared observables
    for oid, cids in obs_cases.items():
        otype = obs[oid]["type"]
        if otype in NEVER_LINK or len(cids) < 2:
            continue
        if len(cids) > hub_cap:
            log.info("hub observable ignored: %s %s (%d cases)", otype, obs[oid]["value"], len(cids))
            G.graph["hubs"].add(oid)
            continue
        weight = 0.4 if otype in weak else (0.8 if otype == "phrase" else 1.0)
        cl = sorted(cids)
        for i in range(len(cl)):
            for j in range(i + 1, len(cl)):
                a, b = cl[i], cl[j]
                if G.has_edge(a, b):
                    G[a][b]["evidence"].append((otype, obs[oid]["value"], weight))
                else:
                    G.add_edge(a, b, evidence=[(otype, obs[oid]["value"], weight)], sim=0.0)

    # ---- lure wording similarity (lure sentences only; titles like "Is this a
    # scam?" are too short and too common to be evidence)
    texts = {cid: (c["lure_text"] or "") for cid, c in cases.items()}
    pairs = similar_pairs(texts, float(ccfg.get("text_similarity_threshold", 0.35)))
    if ccfg.get("use_embeddings"):
        pairs += embedding_pairs(texts, float(ccfg.get("embedding_similarity_threshold", 0.78)))
    for a, b, s in pairs:
        if G.has_edge(a, b):
            G[a][b]["sim"] = max(G[a][b]["sim"], s)
        else:
            G.add_edge(a, b, evidence=[], sim=s)

    # ---- prune edges that rest on weak evidence of a single kind (two domains on
    # the same cloud IPs is not a link; shared IP *and* shared nameserver might be)
    for a, b, d in list(G.edges(data=True)):
        hard = [e for e in d["evidence"] if e[0] in HARD_TYPES or e[0] == "phrase"]
        weak_kinds = {e[0] for e in d["evidence"] if e[0] in weak}
        if not hard and d["sim"] == 0 and len(weak_kinds) < 2:
            G.remove_edge(a, b)
    return G, cases, obs, obs_cases


def _spec_map(ccfg: dict[str, Any]) -> dict[str, str]:
    table = ccfg.get("specificity") or DEFAULT_SPECIFICITY
    return {t: level for level, types in table.items() for t in types}


def _parent_domains(conn: sqlite3.Connection) -> dict[str, str]:
    """observable id -> the domain value it was derived from (url -> host, ip/ns/registrar -> domain)."""
    parent: dict[str, str] = {}
    dom_value = {r["id"]: r["value"] for r in conn.execute("SELECT id, value FROM observables WHERE type='domain'")}
    for r in conn.execute(
            "SELECT src_type, src_id, predicate, dst_type, dst_id FROM relationships WHERE predicate IN "
            "('resolves-to', 'uses-ns', 'registered-via', 'hosted-on')"):
        if r["predicate"] == "hosted-on" and r["dst_id"] in dom_value:      # url -> domain
            parent[r["src_id"]] = dom_value[r["dst_id"]]
        elif r["src_id"] in dom_value:                                       # domain -> ip/ns/registrar
            parent.setdefault(r["dst_id"], dom_value[r["src_id"]])
    return parent


def confidence_from_evidence(evidence: list[dict[str, Any]], single_reporter: bool = False) -> tuple[str, str]:
    """Ladder: count *independent groups* by their best specificity.

    strongly_clustered: >=2 high groups, or 1 high + >=2 medium
    probable:           1 high group, or >=2 medium groups
    possible:           anything else that still formed a cluster
    Single-reporter clusters are capped at `possible`."""
    rank = {"high": 3, "medium": 2, "low": 1}
    best: dict[str, str] = {}
    for e in evidence:
        g = e["group"]
        if rank[e["specificity"]] > rank.get(best.get(g, "low"), 0) or g not in best:
            best[g] = e["specificity"]
    high = sum(1 for v in best.values() if v == "high")
    med = sum(1 for v in best.values() if v == "medium")
    low = sum(1 for v in best.values() if v == "low")
    if high >= 2 or (high >= 1 and med >= 2):
        level = "strongly_clustered"
    elif high >= 1 or med >= 2:
        level = "probable"
    else:
        level = "possible"
    basis = f"{high} independent high-specificity, {med} medium, {low} low"
    if single_reporter and level != "possible":
        level = "possible"
        basis += "; capped: single reporter"
    return level, basis


def summarise(conn: sqlite3.Connection, G: nx.Graph, members: list[str], cases: dict[str, dict[str, Any]],
              obs: dict[str, dict[str, Any]], obs_cases: dict[str, set[str]],
              spec: dict[str, str] | None = None, parents: dict[str, str] | None = None,
              labels: dict[str, str] | None = None, hub_ids: set[str] | None = None) -> dict[str, Any]:
    n = len(members)
    mset = set(members)
    spec = spec or _spec_map({})
    parents = parents if parents is not None else _parent_domains(conn)
    labels = labels or {}
    hub_ids = hub_ids or set()
    shared: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for oid, cids in obs_cases.items():
        k = len(cids & mset)
        if k >= 2:
            shared[obs[oid]["type"]].append((obs[oid]["value"], k))
    for t in shared:
        shared[t].sort(key=lambda x: -x[1])

    brands: Counter[str] = Counter()
    for oid, cids in obs_cases.items():
        if obs[oid]["type"] == "brand" and cids & mset:
            brands[obs[oid]["value"]] += len(cids & mset)
    repos = sorted({obs[oid]["value"] for oid, cids in obs_cases.items()
                    if obs[oid]["type"] == "github_repo" and cids & mset})
    lure_types = Counter(cases[c]["lure_type"] for c in members if cases[c]["lure_type"])

    # evidence counts across intra-cluster edges
    hard_pairs = sim_pairs = weak_pairs = 0
    hard_items: Counter[str] = Counter()
    for a, b, d in G.subgraph(members).edges(data=True):
        kinds = {e[0] for e in d["evidence"]}
        if kinds & HARD_TYPES:
            hard_pairs += 1
            for e in d["evidence"]:
                if e[0] in HARD_TYPES:
                    hard_items[f"{e[0]}:{e[1]}"] += 1
        if d["sim"] > 0 or "phrase" in kinds:
            sim_pairs += 1
        if kinds & WEAK_DEFAULT:
            weak_pairs += 1

    # how many cases share wording with at least one other member
    wording_cases = {c for a, b, d in G.subgraph(members).edges(data=True)
                     if d["sim"] > 0 or any(e[0] == "phrase" for e in d["evidence"]) for c in (a, b)}
    infra_cases = {c for a, b, d in G.subgraph(members).edges(data=True)
                   if any(e[0] in HARD_TYPES for e in d["evidence"]) for c in (a, b)}

    # ---- structured evidence: one item per shared feature, grouped by independence
    evidence: list[dict[str, Any]] = []
    for oid, cids in obs_cases.items():
        k = cids & mset
        if len(k) < 2 or obs[oid]["type"] in NEVER_LINK or oid in hub_ids:
            continue
        t, v = obs[oid]["type"], obs[oid]["value"]
        evidence.append({"feature": t, "value": v, "specificity": spec.get(t, "low"),
                         "cases": sorted(k), "group": parents.get(oid, v)})
    sim_edges = [(a, b, d["sim"]) for a, b, d in G.subgraph(members).edges(data=True) if d["sim"] > 0]
    if sim_edges:
        evidence.append({"feature": "wording_similarity",
                         "value": f"{len(sim_edges)} similar pair(s), max jaccard {max(e[2] for e in sim_edges):.2f}",
                         "specificity": spec.get("wording_similarity", "medium"),
                         "cases": sorted({c for a, b, _ in sim_edges for c in (a, b)}), "group": "wording"})
    order = {"high": 0, "medium": 1, "low": 2}
    evidence.sort(key=lambda e: (order[e["specificity"]], -len(e["cases"]), e["feature"], e["value"]))
    authors = {cases[m].get("author") for m in members if cases[m].get("author")}
    single_reporter = n > 1 and len(authors) == 1
    confidence, basis = confidence_from_evidence(evidence, single_reporter)

    reasons: list[str] = []
    if wording_cases:
        reasons.append(f"{len(wording_cases)}/{n} reports share {labels.get('wording', 'lure wording')}")
    if infra_cases:
        reasons.append(f"{len(infra_cases)}/{n} reference the same infrastructure or contact")
    for key, cnt in hard_items.most_common(4):
        t, v = key.split(":", 1)
        k = len(obs_cases[next(o for o in obs_cases if obs[o]["type"] == t and obs[o]["value"] == v)] & mset)
        reasons.append(f"{k} cases share {HUMAN_TYPE.get(t, t)} {v}")
    if shared.get("dependency"):
        d, k = shared["dependency"][0]
        reasons.append(f"{k} GitHub repositories share the dependency {d}")
    if shared.get("ip") and not shared.get("domain"):
        ip, k = shared["ip"][0]
        reasons.append(f"{k} cases reference infrastructure on {ip}")
    if len(lure_types) == 1:
        reasons.append(f"all cases classified as {next(iter(lure_types))}")
    if single_reporter:
        reasons.append("all posts by the same account (cross-post or repeat report)")

    # common lure: most shared phrase, else the lure sentence appearing most
    common_lure = None
    if shared.get("phrase"):
        common_lure = shared["phrase"][0][0]
    else:
        c: Counter[str] = Counter()
        for m in members:
            for line in (cases[m]["lure_text"] or "").split("\n")[:3]:
                if line:
                    c[line] += 1
        if c:
            common_lure = c.most_common(1)[0][0]

    # enrichment hints
    young: list[str] = []
    for value, _ in shared.get("domain", []) + [(obs[o]["value"], 1) for o in obs_cases
                                                if obs[o]["type"] == "domain" and obs_cases[o] & mset]:
        row = conn.execute(
            "SELECT e.result FROM enrichment e JOIN observables o ON o.id = e.observable_id "
            "WHERE o.type='domain' AND o.value=? AND e.provider='rdap'", (value,)).fetchone()
        if row and row["result"]:
            try:
                if json.loads(row["result"]).get("newly_registered") and value not in young:
                    young.append(value)
            except json.JSONDecodeError:
                pass

    first_seen = min((cases[m]["published_at"] for m in members if cases[m]["published_at"]), default=None)
    summary = {
        "size": n,
        "first_seen": first_seen,
        "common_lure": common_lure,
        "lure_types": dict(lure_types.most_common()),
        "brands": [c for c, _ in brands.most_common(6)],
        "shared_domains": [v for v, _ in shared.get("domain", [])[:6]],
        "shared_urls": [v for v, _ in shared.get("url", [])[:4]],
        "shared_telegram": [v for v, _ in shared.get("telegram", [])[:4]],
        "shared_emails": [v for v, _ in shared.get("email", [])[:4]],
        "shared_wallets": [v for v, _ in (shared.get("wallet_eth", []) + shared.get("wallet_btc", [])
                                          + shared.get("wallet_trx", []))[:4]],
        "shared_ips": [v for v, _ in shared.get("ip", [])[:4]],
        "shared_dependencies": [v for v, _ in shared.get("dependency", [])[:4]],
        "repos": repos[:10],
        "young_domains": young[:6],
        "sources": dict(Counter(cases[m]["source"].split(":")[0] for m in members)),
        "evidence": evidence,
        "edge_counts": {"hard_pairs": hard_pairs, "sim_pairs": sim_pairs, "weak_pairs": weak_pairs},
        "reasons": reasons,
        "confidence": confidence,
        "confidence_basis": basis,
        "single_reporter": single_reporter,
        "cases": sorted(members),
    }
    summary["pivots"] = pivots_for_cluster(summary, labels)
    return summary


def _label(summary: dict[str, Any]) -> str:
    bits = []
    if summary["brands"]:
        bits.append("/".join(summary["brands"][:2]))
    if summary["lure_types"]:
        bits.append(next(iter(summary["lure_types"])).replace("_", " "))
    if summary["shared_telegram"]:
        bits.append("@" + summary["shared_telegram"][0])
    elif summary["shared_domains"]:
        bits.append(summary["shared_domains"][0])
    return " · ".join(bits) or (summary["common_lure"] or "")[:60] or "unlabelled"


def persist(conn: sqlite3.Connection, components: list[list[str]], summaries: list[dict[str, Any]]) -> dict[str, int]:
    existing = defaultdict(set)
    for r in conn.execute("SELECT cluster_id, case_id FROM cluster_cases"):
        existing[r["cluster_id"]].add(r["case_id"])
    stats = {"new": 0, "updated": 0, "unchanged": 0, "total": len(components)}
    claimed: set[str] = set()
    ts = db.now_iso()
    for members, summary in zip(components, summaries):
        mset = set(members)
        best, overlap = None, 0
        for cid, cases in existing.items():
            if cid in claimed:
                continue
            k = len(cases & mset)
            if k > overlap:
                best, overlap = cid, k
        blob = json.dumps(summary, ensure_ascii=False)
        if best and overlap >= max(1, len(existing[best]) // 2):
            claimed.add(best)
            changed = existing[best] != mset
            conn.execute("DELETE FROM cluster_cases WHERE cluster_id = ?", (best,))
            conn.executemany("INSERT INTO cluster_cases(cluster_id, case_id) VALUES (?,?)",
                             [(best, m) for m in members])
            conn.execute(
                "UPDATE clusters SET updated_at=?, size=?, confidence=?, label=?, first_seen=?, summary=?, "
                "is_new = CASE WHEN ? THEN 1 ELSE is_new END WHERE id=?",
                (ts, len(members), summary["confidence"], _label(summary), summary["first_seen"], blob,
                 int(changed), best))
            stats["updated" if changed else "unchanged"] += 1
        else:
            cid = db.next_id(conn, "CLU")
            conn.execute(
                "INSERT INTO clusters(id, created_at, updated_at, size, confidence, label, first_seen, summary, "
                "status, is_new) VALUES (?,?,?,?,?,?,?,?,'new',1)",
                (cid, ts, ts, len(members), summary["confidence"], _label(summary), summary["first_seen"], blob))
            conn.executemany("INSERT INTO cluster_cases(cluster_id, case_id) VALUES (?,?)",
                             [(cid, m) for m in members])
            stats["new"] += 1
    # clusters that dissolved (all members re-assigned) are closed, not deleted
    for cid in existing:
        if cid not in claimed:
            conn.execute("UPDATE clusters SET status='closed', is_new=0, updated_at=? WHERE id=? AND status!='closed'",
                         (ts, cid))
    return stats


def run(conn: sqlite3.Connection, sources_cfg: dict[str, Any], lexicon: dict[str, Any] | None = None) -> dict[str, int]:
    ccfg = sources_cfg.get("clustering", {})
    if lexicon is None:
        from .. import config
        try:
            lexicon = config.lexicon()
        except FileNotFoundError:
            lexicon = {}
    labels = (lexicon or {}).get("labels") or {}
    G, cases, obs, obs_cases = build_graph(conn, ccfg)
    min_size = int(ccfg.get("min_cluster_size", 2))
    components = [sorted(c) for c in nx.connected_components(G) if len(c) >= min_size]
    components.sort(key=lambda c: (-len(c), c[0]))
    spec, parents = _spec_map(ccfg), _parent_domains(conn)
    summaries = [summarise(conn, G, c, cases, obs, obs_cases, spec, parents, labels, G.graph["hubs"])
                 for c in components]
    with db.tx(conn):
        stats = persist(conn, components, summaries)
    stats["cases_in_graph"] = len(cases)
    stats["edges"] = G.number_of_edges()
    log.info("clustering: %s", stats)
    return stats


def cluster_bundle(conn: sqlite3.Connection, cluster_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
    if not row:
        return None
    summary = json.loads(row["summary"] or "{}")
    cases = [dict(r) for r in conn.execute(
        "SELECT c.id, c.source, c.url, c.title, c.published_at, c.lure_type, c.stage, c.lure_text, "
        "substr(c.body, 1, 1500) AS excerpt FROM cluster_cases cc JOIN cases c ON c.id = cc.case_id "
        "WHERE cc.cluster_id = ? ORDER BY c.published_at", (cluster_id,))]
    return {"cluster": dict(row), "summary": summary, "cases": cases}
