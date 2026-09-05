"""GitHub repository search collector.

Unauthenticated search is limited to 10 requests/minute; set GITHUB_TOKEN to
raise that and to allow README fetches at scale.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .. import config
from ..http import get
from .base import Collector, RawItem, iso, truncate

log = logging.getLogger("cdisc.collect.github")


def gh_headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = config.env("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


class GitHubCollector(Collector):
    name = "github"
    kind = "github-search"

    _readme_calls = 0

    def _api(self) -> str:
        return self.cfg.get("api", "https://api.github.com").rstrip("/")

    def _search(self, q: str) -> list[dict[str, Any]]:
        try:
            resp = get(f"{self._api()}/search/repositories",
                       params={"q": q, "sort": "updated", "order": "desc",
                               "per_page": self.cfg.get("per_page", 30)},
                       headers=gh_headers(), timeout=30, min_interval=6.5, key="github-search")
            if resp.status_code != 200:
                log.warning("github search %r -> %s %s", q, resp.status_code, resp.text[:200])
                return []
            return resp.json().get("items", [])
        except Exception as exc:
            log.warning("github search %r: %s", q, exc)
            return []

    def _readme(self, full_name: str) -> str:
        try:
            resp = get(f"{self._api()}/repos/{full_name}/readme", headers=gh_headers(),
                       timeout=20, min_interval=0.8, key="github-api")
            if resp.status_code != 200:
                return ""
            data = resp.json()
            if data.get("encoding") == "base64":
                return base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
        except Exception as exc:
            log.debug("readme %s: %s", full_name, exc)
        return ""

    def _to_item(self, r: dict[str, Any], generation: int = 0) -> RawItem:
        full = r["full_name"]
        desc = r.get("description") or ""
        # Unauthenticated REST calls are capped at 60/hour, so README fetches are
        # budgeted per run unless a token is present.
        budget = self.cfg.get("readme_max_per_run", 20) if not config.env("GITHUB_TOKEN") else 10**6
        readme = ""
        if self.cfg.get("fetch_readme", True) and self._readme_calls < budget:
            self._readme_calls += 1
            readme = self._readme(full)
        body = f"{desc}\n\nRepository: https://github.com/{full}\nOwner: {r['owner']['login']}\n"
        if r.get("homepage"):
            body += f"Homepage: {r['homepage']}\n"
        if r.get("topics"):
            body += "Topics: " + ", ".join(r["topics"]) + "\n"
        if readme:
            body += "\n--- README ---\n" + readme
        return RawItem(
            source="github",
            source_ref=full.lower(),
            title=f"{full}: {desc[:120]}" if desc else full,
            body=truncate(body, 30000),
            url=r.get("html_url"),
            author=r["owner"]["login"],
            published_at=iso(r.get("created_at")),
            generation=generation,
            raw={k: r.get(k) for k in ("id", "full_name", "description", "html_url", "created_at",
                                       "updated_at", "pushed_at", "stargazers_count", "forks_count",
                                       "language", "topics", "homepage", "fork", "default_branch")}
                | {"owner": {k: r["owner"].get(k) for k in ("login", "id", "type", "html_url")}},
        )

    def collect(self, since: datetime | None = None) -> Iterator[RawItem]:
        days = int(self.cfg.get("lookback_days", 30))
        since_date = (since or datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        seen: set[str] = set()
        for q in self.cfg.get("repo_queries", []):
            for r in self._search(q.replace("{since}", since_date)):
                if r["full_name"].lower() in seen:
                    continue
                seen.add(r["full_name"].lower())
                yield self._to_item(r)

    def search(self, term: str, since: datetime | None = None, generation: int = 1) -> Iterator[RawItem]:
        q = f'{term} in:readme,description'
        if since:
            q += f" pushed:>{since.strftime('%Y-%m-%d')}"
        for r in self._search(q):
            yield self._to_item(r, generation=generation)
