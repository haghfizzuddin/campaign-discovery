"""URLhaus recent-URL feed, filtered to the profile's campaign tags.

The CSV download needs no key. Rows are only kept when the `threat` or `tags`
column contains one of the configured substrings (BeaverTail, Lazarus, ...).
Items from this collector are *trusted*: they bypass the relevance gate.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Iterator

from ..http import get
from .base import Collector, RawItem, iso

log = logging.getLogger("cdisc.collect.urlhaus")

COLUMNS = ["id", "dateadded", "url", "url_status", "last_online", "threat", "tags", "urlhaus_link", "reporter"]


class URLhausCollector(Collector):
    name = "urlhaus"
    kind = "cti-feed"

    def collect(self, since: datetime | None = None) -> Iterator[RawItem]:
        url = self.cfg.get("csv_url", "https://urlhaus.abuse.ch/downloads/csv_recent/")
        filters = [f.lower() for f in self.cfg.get("tag_filters", [])]
        try:
            resp = get(url, timeout=60)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("urlhaus: %s", exc)
            return
        lines = [l for l in resp.text.splitlines() if l and not l.startswith("#")]
        reader = csv.reader(io.StringIO("\n".join(lines)))
        for row in reader:
            if len(row) < 9:
                continue
            rec = dict(zip(COLUMNS, row))
            hay = f"{rec['threat']} {rec['tags']}".lower()
            if filters and not any(f in hay for f in filters):
                continue
            published = iso(rec["dateadded"].replace(" ", "T") + "+00:00")
            if since and published and published < since.isoformat():
                continue
            yield RawItem(
                source="urlhaus",
                source_ref=rec["id"],
                title=f"URLhaus {rec['id']}: {rec['threat']} [{rec['tags']}]",
                body=(f"URL: {rec['url']}\nStatus: {rec['url_status']}\nThreat: {rec['threat']}\n"
                      f"Tags: {rec['tags']}\nReporter: {rec['reporter']}\nReference: {rec['urlhaus_link']}"),
                url=rec["urlhaus_link"],
                author=rec["reporter"],
                published_at=published,
                trusted=True,
                raw=rec,
            )
