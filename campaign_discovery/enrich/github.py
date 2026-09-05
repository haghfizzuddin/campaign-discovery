"""GitHub repository / account enrichment.

Pulls repo + owner metadata and dependency manifests (package.json,
requirements.txt). Dependencies become `dependency` observables linked to the
same cases, so two repos that share an unusual package cluster together.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import db
from ..collectors.github import gh_headers
from ..http import get

log = logging.getLogger("cdisc.enrich.github")

# Dependencies too common to carry linking signal.
COMMON_DEPS = {
    "react", "react-dom", "express", "axios", "lodash", "typescript", "next", "vite", "webpack",
    "eslint", "prettier", "jest", "dotenv", "cors", "mongoose", "body-parser", "nodemon", "vue",
    "tailwindcss", "postcss", "autoprefixer", "@types/node", "@types/react", "@types/react-dom",
    "react-router-dom", "redux", "@reduxjs/toolkit", "styled-components", "sass", "moment", "uuid",
    "requests", "numpy", "pandas", "flask", "django", "pytest", "fastapi", "uvicorn", "pydantic",
    "sqlalchemy", "python-dotenv", "beautifulsoup4", "matplotlib", "scikit-learn", "torch",
}


def _get(path: str) -> Any:
    resp = get(f"https://api.github.com{path}", headers=gh_headers(), timeout=20,
               min_interval=0.8, key="github-api")
    if resp.status_code == 404:
        return None
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        raise RuntimeError("github rate limit; set GITHUB_TOKEN")
    resp.raise_for_status()
    return resp.json()


def _file(full: str, name: str) -> str | None:
    data = _get(f"/repos/{full}/contents/{name}")
    if not data or data.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
    except Exception:
        return None


def _deps(full: str) -> list[str]:
    deps: list[str] = []
    pkg = _file(full, "package.json")
    if pkg:
        try:
            j = json.loads(pkg)
            for k in ("dependencies", "devDependencies"):
                deps += [f"npm:{d}" for d in (j.get(k) or {})]
        except json.JSONDecodeError:
            pass
    req = _file(full, "requirements.txt")
    if req:
        for line in req.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "-")):
                deps.append("pypi:" + re.split(r"[=<>!~\[; ]", line, 1)[0].lower())
    return sorted(set(deps))


def _age(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days
    except ValueError:
        return None


def fetch(obs_type: str, value: str) -> dict[str, Any]:
    if obs_type == "github_user":
        u = _get(f"/users/{value}")
        if not u:
            return {"found": False}
        return {
            "found": True, "login": u.get("login"), "type": u.get("type"), "created_at": u.get("created_at"),
            "account_age_days": _age(u.get("created_at")), "public_repos": u.get("public_repos"),
            "followers": u.get("followers"), "name": u.get("name"), "company": u.get("company"),
            "blog": u.get("blog"), "location": u.get("location"), "bio": u.get("bio"),
            "young_account": (_age(u.get("created_at")) or 9999) <= 90,
        }
    r = _get(f"/repos/{value}")
    if not r:
        return {"found": False}
    return {
        "found": True, "full_name": r.get("full_name"), "description": r.get("description"),
        "created_at": r.get("created_at"), "pushed_at": r.get("pushed_at"),
        "repo_age_days": _age(r.get("created_at")), "stars": r.get("stargazers_count"),
        "forks": r.get("forks_count"), "language": r.get("language"), "fork": r.get("fork"),
        "archived": r.get("archived"), "homepage": r.get("homepage"), "topics": r.get("topics"),
        "owner": (r.get("owner") or {}).get("login"), "dependencies": _deps(value),
        "young_repo": (_age(r.get("created_at")) or 9999) <= 60,
    }


def apply(conn: sqlite3.Connection, obs: sqlite3.Row, result: dict[str, Any]) -> None:
    """Uncommon dependencies and the owner become observables linked to the same cases."""
    if obs["type"] != "github_repo" or not (result or {}).get("found"):
        return
    full = obs["value"]
    cases = [row["case_id"] for row in conn.execute(
        "SELECT DISTINCT case_id FROM case_observables WHERE observable_id = ?", (obs["id"],))]
    for d in result.get("dependencies") or []:
        if d.split(":", 1)[-1].lower() in COMMON_DEPS:
            continue
        d_id = db.upsert_observable(conn, "dependency", d)
        db.add_relationship(conn, "github_repo", obs["id"], "depends-on", "dependency", d_id)
        for c in cases:
            db.link_case_observable(conn, c, d_id, "links-to", 0.5, f"dependency of {full}")
    owner = result.get("owner")
    if owner:
        o_id = db.upsert_observable(conn, "github_user", owner.lower())
        db.add_relationship(conn, "github_repo", obs["id"], "owned-by", "github_user", o_id)
        for c in cases:
            db.link_case_observable(conn, c, o_id, "delivers", 0.9, f"owner of {full}")


def lookup(conn: sqlite3.Connection, obs: sqlite3.Row) -> dict[str, Any] | None:
    result = fetch(obs["type"], obs["value"])
    apply(conn, obs, result)
    return result
