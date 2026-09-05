"""Mastodon public hashtag timelines. Instances requiring auth are skipped."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterator

from ..http import get, html_to_text
from .base import Collector, RawItem, iso, truncate

log = logging.getLogger("cdisc.collect.mastodon")


class MastodonCollector(Collector):
    name = "mastodon"
    kind = "mastodon"

    def collect(self, since: datetime | None = None) -> Iterator[RawItem]:
        seen: set[str] = set()
        for inst in self.cfg.get("instances", []):
            inst = inst.rstrip("/")
            for tag in self.cfg.get("tags", []):
                try:
                    resp = get(f"{inst}/api/v1/timelines/tag/{tag}",
                               params={"limit": self.cfg.get("limit", 40)}, timeout=20,
                               min_interval=1.0, key=inst)
                except Exception as exc:
                    log.warning("mastodon %s #%s: %s", inst, tag, exc)
                    continue
                if resp.status_code != 200:
                    log.info("mastodon %s #%s -> %s (skipped)", inst, tag, resp.status_code)
                    continue
                for s in resp.json():
                    uri = s.get("uri") or s.get("url")
                    if not uri or uri in seen:
                        continue
                    seen.add(uri)
                    published = iso(s.get("created_at"))
                    if since and published and published < since.isoformat():
                        continue
                    text = html_to_text(s.get("content", ""))
                    acct = (s.get("account") or {}).get("acct")
                    yield RawItem(
                        source="mastodon",
                        source_ref=uri,
                        title=text.split("\n", 1)[0][:140],
                        body=truncate(text),
                        url=s.get("url") or uri,
                        author=acct,
                        published_at=published,
                        raw={"uri": uri, "tag": tag, "instance": inst, "account": acct,
                             "content": s.get("content"), "created_at": s.get("created_at"),
                             "tags": [t.get("name") for t in s.get("tags", [])]},
                    )
