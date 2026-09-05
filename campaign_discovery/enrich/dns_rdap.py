"""DNS (dnspython) and RDAP (rdap.org bootstrap) lookups for domains."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import dns.exception
import dns.resolver

from .. import db
from ..http import get

log = logging.getLogger("cdisc.enrich.dns")

_resolver = dns.resolver.Resolver()
_resolver.lifetime = 6.0
_resolver.timeout = 3.0


def _q(name: str, rtype: str) -> list[str]:
    try:
        ans = _resolver.resolve(name, rtype)
        return sorted(str(r).rstrip(".").lower() for r in ans)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers,
            dns.exception.Timeout, dns.resolver.YXDOMAIN):
        return []
    except Exception as exc:  # pragma: no cover
        log.debug("dns %s %s: %s", name, rtype, exc)
        return []


def dns_fetch(domain: str) -> dict[str, Any]:
    a = _q(domain, "A")
    return {
        "a": a,
        "aaaa": _q(domain, "AAAA"),
        "ns": _q(domain, "NS"),
        "mx": [m.split(" ", 1)[-1] for m in _q(domain, "MX")],
        "cname": _q(domain, "CNAME"),
        "resolves": bool(a),
    }


def dns_apply(conn: sqlite3.Connection, obs: sqlite3.Row, result: dict[str, Any]) -> None:
    """Derived observables so clustering can pivot on shared infrastructure."""
    domain = obs["value"]
    cases = [r["case_id"] for r in conn.execute(
        "SELECT DISTINCT case_id FROM case_observables WHERE observable_id = ?", (obs["id"],))]
    for ip in (result.get("a") or [])[:5]:
        ip_id = db.upsert_observable(conn, "ip", ip)
        db.add_relationship(conn, "domain", obs["id"], "resolves-to", "ip", ip_id)
        for c in cases:
            db.link_case_observable(conn, c, ip_id, "links-to", 0.6, f"A record of {domain}")
    for ns in (result.get("ns") or [])[:4]:
        ns_id = db.upsert_observable(conn, "nameserver", ns)
        db.add_relationship(conn, "domain", obs["id"], "uses-ns", "nameserver", ns_id)
        for c in cases:
            db.link_case_observable(conn, c, ns_id, "links-to", 0.4, f"NS of {domain}")


def dns_lookup(conn: sqlite3.Connection, obs: sqlite3.Row) -> dict[str, Any]:
    result = dns_fetch(obs["value"])
    dns_apply(conn, obs, result)
    return result


def _registrable(domain: str) -> str:
    """Cheap eTLD+1 guess: last two labels, or three for common ccSLDs."""
    parts = domain.lower().split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def rdap_fetch(domain: str) -> dict[str, Any]:
    registrable = _registrable(domain)
    resp = get(f"https://rdap.org/domain/{registrable}", timeout=25, min_interval=1.0, key="rdap",
               headers={"Accept": "application/rdap+json, application/json"})
    if resp.status_code == 404:
        return {"registrable": registrable, "found": False}
    if resp.status_code != 200:
        return {"registrable": registrable, "found": False, "http": resp.status_code}
    data = resp.json()
    events = {e.get("eventAction"): e.get("eventDate") for e in data.get("events", [])}
    registrar = None
    for ent in data.get("entities", []):
        if "registrar" in (ent.get("roles") or []):
            for item in (ent.get("vcardArray") or [None, []])[1]:
                if item and item[0] == "fn":
                    registrar = item[3]
            registrar = registrar or ent.get("handle")
    created = events.get("registration")
    age_days = None
    if created:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).days
        except ValueError:
            pass
    return {
        "registrable": registrable,
        "found": True,
        "registrar": registrar,
        "created": created,
        "expires": events.get("expiration"),
        "updated": events.get("last changed"),
        "age_days": age_days,
        "newly_registered": age_days is not None and age_days <= 90,
        "status": data.get("status", []),
        "nameservers": sorted((ns.get("ldhName") or "").lower() for ns in data.get("nameservers", [])),
    }


def rdap_apply(conn: sqlite3.Connection, obs: sqlite3.Row, result: dict[str, Any]) -> None:
    registrar = (result or {}).get("registrar")
    if registrar:
        reg_id = db.upsert_observable(conn, "registrar", registrar)
        db.add_relationship(conn, "domain", obs["id"], "registered-via", "registrar", reg_id)


def rdap_lookup(conn: sqlite3.Connection, obs: sqlite3.Row) -> dict[str, Any]:
    result = rdap_fetch(obs["value"])
    rdap_apply(conn, obs, result)
    return result
