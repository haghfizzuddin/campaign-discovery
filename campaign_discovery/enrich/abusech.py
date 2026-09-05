"""abuse.ch lookups: URLhaus (host/url) and ThreatFox (IOC search).

abuse.ch APIs require a free Auth-Key since 2025 (https://auth.abuse.ch/).
Without ABUSECH_AUTH_KEY the provider raises Unauthorized once and is skipped.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .. import config
from ..http import post


class Unauthorized(Exception):
    pass


def _headers() -> dict[str, str]:
    key = config.env("ABUSECH_AUTH_KEY")
    if not key:
        raise Unauthorized("ABUSECH_AUTH_KEY not set")
    return {"Auth-Key": key}


def _post(url: str, **kw: Any) -> dict[str, Any]:
    resp = post(url, headers=_headers(), timeout=30, min_interval=1.0, key="abusech", **kw)
    if resp.status_code in (401, 403):
        raise Unauthorized(f"{url} -> {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def fetch(obs_type: str, value: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if obs_type in ("domain", "ip"):
        uh = _post("https://urlhaus-api.abuse.ch/v1/host/", data={"host": value})
        out["urlhaus"] = {
            "status": uh.get("query_status"),
            "url_count": uh.get("url_count"),
            "blacklists": uh.get("blacklists"),
            "urls": [{"url": u.get("url"), "threat": u.get("threat"), "tags": u.get("tags"),
                      "date_added": u.get("date_added")} for u in (uh.get("urls") or [])[:10]],
        }
    elif obs_type == "url":
        uh = _post("https://urlhaus-api.abuse.ch/v1/url/", data={"url": value})
        out["urlhaus"] = {"status": uh.get("query_status"), "threat": uh.get("threat"),
                          "tags": uh.get("tags"), "date_added": uh.get("date_added")}
    if obs_type in ("domain", "ip", "url", "sha256", "md5"):
        tf = _post("https://threatfox-api.abuse.ch/api/v1/",
                   json={"query": "search_ioc", "search_term": value, "exact_match": True})
        data = tf.get("data") if isinstance(tf.get("data"), list) else []
        out["threatfox"] = {
            "status": tf.get("query_status"),
            "iocs": [{"ioc": d.get("ioc"), "type": d.get("ioc_type"), "malware": d.get("malware_printable"),
                      "threat_type": d.get("threat_type"), "confidence": d.get("confidence_level"),
                      "first_seen": d.get("first_seen"), "tags": d.get("tags"), "reporter": d.get("reporter")}
                     for d in data[:10]],
        }
    return out


def apply(conn: sqlite3.Connection, obs: sqlite3.Row, result: dict[str, Any]) -> None:
    from .. import db
    malware = {i.get("malware") for i in ((result or {}).get("threatfox") or {}).get("iocs", []) if i.get("malware")}
    for m in sorted(malware):
        m_id = db.upsert_observable(conn, "malware_family", m)
        db.add_relationship(conn, obs["type"], obs["id"], "associated-with", "malware_family", m_id)


def lookup(conn: sqlite3.Connection, obs: sqlite3.Row) -> dict[str, Any]:
    result = fetch(obs["type"], obs["value"])
    apply(conn, obs, result)
    return result
