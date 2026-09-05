"""Enrichment runner: deterministic lookups for observables.

Providers are independent; each returns a JSON-serialisable dict that is stored
in the `enrichment` table. Some also create derived observables (resolved IPs,
nameservers, dependencies) so the clustering step can use them as evidence.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import config, db
from . import abusech, dns_rdap, github, intelowl

log = logging.getLogger("cdisc.enrich")

# provider name -> (observable types it applies to, lookup = fetch + apply)
PROVIDERS: dict[str, tuple[tuple[str, ...], Callable[..., dict[str, Any] | None]]] = {
    "dns": (("domain",), dns_rdap.dns_lookup),
    "rdap": (("domain",), dns_rdap.rdap_lookup),
    "abusech": (("domain", "url", "ip", "sha256", "md5"), abusech.lookup),
    "github": (("github_repo", "github_user"), github.lookup),
    "intelowl": (("domain", "url", "ip", "sha256"), intelowl.lookup),
}
# provider -> apply(conn, obs, stored_result): re-creates derived observables
# (IPs, nameservers, registrars, dependencies) from a stored result, no network.
APPLIERS: dict[str, Callable[..., None]] = {
    "dns": dns_rdap.dns_apply,
    "rdap": dns_rdap.rdap_apply,
    "abusech": abusech.apply,
    "github": github.apply,
    "intelowl": intelowl.apply,
}


def snapshot(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Enrichment rows keyed by (type, value) so they survive a reprocess."""
    return [dict(r) for r in conn.execute(
        "SELECT o.type, o.value, e.provider, e.fetched_at, e.ok, e.result FROM enrichment e "
        "JOIN observables o ON o.id = e.observable_id")]


def restore(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    """Re-attach snapshot rows to the re-created observables and re-derive links."""
    import json
    n = 0
    for r in rows:
        obs = conn.execute("SELECT * FROM observables WHERE type = ? AND value = ?",
                           (r["type"], r["value"])).fetchone()
        if not obs:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO enrichment(observable_id, provider, fetched_at, ok, result) VALUES (?,?,?,?,?)",
            (obs["id"], r["provider"], r["fetched_at"], r["ok"], r["result"]))
        n += 1
        if r["ok"] and r["result"] and r["provider"] in APPLIERS:
            try:
                APPLIERS[r["provider"]](conn, obs, json.loads(r["result"]))
            except Exception as exc:  # pragma: no cover
                log.debug("reapply %s %s: %s", r["provider"], r["value"], exc)
    conn.commit()
    return n


def run(conn: sqlite3.Connection, sources_cfg: dict[str, Any], providers: list[str] | None = None,
        limit: int | None = None) -> dict[str, int]:
    ecfg = sources_cfg.get("enrichment", {})
    ttl = timedelta(days=int(ecfg.get("ttl_days", 7)))
    limit = limit or int(ecfg.get("max_per_run", 150))
    active = [p for p in PROVIDERS if ecfg.get(p, True)] if not providers else providers
    cutoff = (datetime.now(timezone.utc) - ttl).isoformat()
    stats = {"lookups": 0, "ok": 0, "failed": 0, "skipped": 0}
    disabled: set[str] = set()

    for provider in active:
        if provider not in PROVIDERS:
            log.warning("unknown provider %s", provider)
            continue
        types, fn = PROVIDERS[provider]
        plimit = limit
        if provider == "github" and not config.env("GITHUB_TOKEN"):
            plimit = min(limit, int(ecfg.get("github_max_without_token", 15)))
        placeholders = ",".join("?" * len(types))
        rows = conn.execute(
            f"SELECT o.* FROM observables o LEFT JOIN enrichment e "
            f"ON e.observable_id = o.id AND e.provider = ? "
            f"WHERE o.type IN ({placeholders}) AND (e.fetched_at IS NULL OR e.fetched_at < ?) "
            f"ORDER BY o.case_count DESC, o.last_seen DESC LIMIT ?",
            (provider, *types, cutoff, plimit),
        ).fetchall()
        for row in rows:
            if provider in disabled:
                stats["skipped"] += 1
                continue
            stats["lookups"] += 1
            try:
                result = fn(conn, row)
            except abusech.Unauthorized:
                log.warning("%s: unauthorized (set ABUSECH_AUTH_KEY); skipping provider", provider)
                disabled.add(provider)
                stats["skipped"] += 1
                continue
            except intelowl.NotConfigured:
                disabled.add(provider)
                stats["skipped"] += 1
                continue
            except Exception as exc:
                log.warning("%s %s: %s", provider, row["value"], exc)
                db.save_enrichment(conn, row["id"], provider, {"error": str(exc)}, ok=False)
                stats["failed"] += 1
                conn.commit()
                continue
            db.save_enrichment(conn, row["id"], provider, result, ok=result is not None)
            stats["ok"] += 1
            conn.commit()
    return stats
