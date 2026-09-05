"""Reddit collector via the Arctic Shift archive API.

Arctic Shift (https://arctic-shift.photon-reddit.com) mirrors Reddit posts and
comments and exposes a free search API. It needs no Reddit credentials, which
matters because public Reddit API access is increasingly restricted.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Iterator

from ..http import get
from .base import Collector, RawItem, iso, truncate

log = logging.getLogger("cdisc.collect.reddit")

_REMOVED = {"[removed]", "[deleted]", "[ Removed by moderator ]", "[ Removed by Reddit ]"}


class RedditArcticCollector(Collector):
    name = "reddit"
    kind = "reddit-archive"

    def _api(self) -> str:
        return self.cfg.get("api", "https://arctic-shift.photon-reddit.com/api").rstrip("/")

    def _posts(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        params = {"limit": self.cfg.get("limit", 100), "sort": "desc", **params}
        interval = float(self.cfg.get("min_interval", 3.0))
        # Full-text `query` searches are expensive for Arctic Shift; it answers
        # 422 "Timeout. Maybe slow down a bit" when pushed. Back off and retry.
        for attempt in range(3):
            try:
                resp = get(f"{self._api()}/posts/search", params=params, timeout=60,
                           min_interval=interval, key="arctic", retries=1)
            except Exception as exc:
                log.warning("arctic %s: %s", params, exc)
                return []
            if resp.status_code == 200:
                return resp.json().get("data", []) or []
            if resp.status_code == 422 and "slow down" in resp.text.lower() and attempt < 2:
                delay = 10 * (attempt + 1)
                log.info("arctic asked to slow down; sleeping %ds", delay)
                time.sleep(delay)
                continue
            log.warning("arctic %s -> %s %s", params, resp.status_code, resp.text[:200])
            return []
        return []

    @staticmethod
    def _to_item(p: dict[str, Any], generation: int = 0) -> RawItem | None:
        body = p.get("selftext") or ""
        title = p.get("title") or ""
        if body in _REMOVED:
            body = ""
        if not title and not body:
            return None
        link_url = p.get("url") or ""
        # keep an outbound link (often the scam site / repo) inside the body
        if link_url and "reddit.com" not in link_url and "redd.it" not in link_url:
            body = f"{body}\n\nLink: {link_url}".strip()
        return RawItem(
            source=f"reddit:{p.get('subreddit') or 'unknown'}",
            source_ref=p["id"],
            title=title,
            body=truncate(body),
            url="https://www.reddit.com" + p["permalink"] if p.get("permalink") else link_url,
            author=p.get("author"),
            published_at=iso(p.get("created_utc")),
            generation=generation,
            raw={k: p.get(k) for k in ("id", "subreddit", "author", "title", "selftext", "url",
                                       "permalink", "created_utc", "score", "num_comments",
                                       "link_flair_text", "_meta")},
        )

    def _paged(self, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Walk backwards in time with `before` until the window is exhausted."""
        max_pages = int(self.cfg.get("max_pages", 5))
        limit = int(self.cfg.get("limit", 100))
        before: int | None = None
        for _ in range(max_pages):
            page_params = dict(params)
            if before:
                page_params["before"] = before
            data = self._posts(page_params)
            yield from data
            if len(data) < limit:
                break
            oldest = min(int(p.get("created_utc") or 0) for p in data)
            if not oldest or (before and oldest >= before):
                break
            before = oldest

    def collect(self, since: datetime | None = None) -> Iterator[RawItem]:
        after = int(since.timestamp()) if since else None
        seen: set[str] = set()
        base: dict[str, Any] = {}
        if after:
            base["after"] = after

        for sub in self.cfg.get("firehose_subreddits", []):
            for p in self._paged({"subreddit": sub, **base}):
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                item = self._to_item(p)
                if item:
                    yield item

        for sub in self.cfg.get("subreddits", []):
            for q in self.cfg.get("queries", []):
                for p in self._posts({"subreddit": sub, "query": q, **base}):
                    if p["id"] in seen:
                        continue
                    seen.add(p["id"])
                    item = self._to_item(p)
                    if item:
                        yield item

    def search(self, term: str, since: datetime | None = None, subreddits: list[str] | None = None,
               generation: int = 1) -> Iterator[RawItem]:
        base: dict[str, Any] = {"query": term}
        if since:
            base["after"] = int(since.timestamp())
        # Arctic Shift rejects `query` without a subreddit or author (HTTP 400)
        targets = subreddits or self.cfg.get("firehose_subreddits") or ["Scams"]
        seen: set[str] = set()
        for sub in targets:
            params = dict(base)
            if sub:
                params["subreddit"] = sub
            for p in self._posts(params):
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                item = self._to_item(p, generation=generation)
                if item:
                    yield item
