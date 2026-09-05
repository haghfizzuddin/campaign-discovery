"""RSS/Atom collector for security blogs and consumer-protection alerts."""
from __future__ import annotations

import calendar
import logging
from datetime import datetime
from typing import Iterator

import feedparser

from ..http import html_to_text, session
from .base import Collector, RawItem, iso, truncate

log = logging.getLogger("cdisc.collect.rss")


class RSSCollector(Collector):
    name = "rss"
    kind = "rss"

    def collect(self, since: datetime | None = None) -> Iterator[RawItem]:
        for feed in self.cfg.get("feeds", []):
            name, url = feed["name"], feed["url"]
            try:
                resp = session().get(url, timeout=30)
                resp.raise_for_status()
            except Exception as exc:
                log.warning("rss %s: %s", name, exc)
                continue
            parsed = feedparser.parse(resp.content)
            for e in parsed.entries:
                # feedparser gives RFC-2822 strings in `published`; the parsed
                # struct_time is the reliable field.
                st = e.get("published_parsed") or e.get("updated_parsed")
                published = iso(calendar.timegm(st)) if st else iso(e.get("published") or e.get("updated"))
                if since and published and published < since.isoformat():
                    continue
                content = ""
                if e.get("content"):
                    content = " ".join(c.get("value", "") for c in e["content"])
                elif e.get("summary"):
                    content = e["summary"]
                yield RawItem(
                    source=f"rss:{name}",
                    source_ref=e.get("id") or e.get("link") or e.get("title", ""),
                    title=e.get("title", ""),
                    body=truncate(html_to_text(content)),
                    url=e.get("link"),
                    author=e.get("author"),
                    published_at=published,
                    raw={k: str(v)[:5000] for k, v in e.items() if k not in ("content", "summary_detail")},
                )
