"""Optional IntelOwl connector (self-hosted). Requires INTELOWL_URL + INTELOWL_API_KEY.

Submits the observable to IntelOwl with its default analyzers and polls for the
job result. Off by default in sources.yaml; enable once you have an instance.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from .. import config
from ..http import get, post


class NotConfigured(Exception):
    pass


def _cfg() -> tuple[str, dict[str, str]]:
    url, key = config.env("INTELOWL_URL"), config.env("INTELOWL_API_KEY")
    if not url or not key:
        raise NotConfigured("INTELOWL_URL / INTELOWL_API_KEY not set")
    return url.rstrip("/"), {"Authorization": f"Token {key}"}


_CLASS = {"domain": "domain", "url": "url", "ip": "ip", "sha256": "hash", "md5": "hash"}


def lookup(conn: sqlite3.Connection, obs: sqlite3.Row, wait_s: int = 90) -> dict[str, Any]:
    base, headers = _cfg()
    payload = {"observables": [[_CLASS[obs["type"]], obs["value"]]], "tlp": "AMBER"}
    resp = post(f"{base}/api/analyze_multiple_observables", json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return {"submitted": False, "response": resp.json()}
    job_id = results[0].get("job_id")
    deadline = time.time() + wait_s
    job: dict[str, Any] = {}
    while time.time() < deadline:
        job = get(f"{base}/api/jobs/{job_id}", headers=headers, timeout=30).json()
        if job.get("status") in ("reported_without_fails", "reported_with_fails", "failed", "killed"):
            break
        time.sleep(5)
    reports = job.get("analyzer_reports") or job.get("analyzers_reports") or []
    return {
        "job_id": job_id, "status": job.get("status"),
        "analyzers": [{"name": r.get("name"), "status": r.get("status"),
                       "report": str(r.get("report"))[:2000]} for r in reports],
        "url": f"{base}/jobs/{job_id}",
    }


def apply(conn: sqlite3.Connection, obs: sqlite3.Row, result: dict[str, Any]) -> None:  # noqa: ARG001
    """IntelOwl results are stored verbatim; nothing derived."""
