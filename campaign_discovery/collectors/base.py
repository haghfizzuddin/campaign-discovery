"""Collector protocol and the RawItem record every collector emits."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .. import config

log = logging.getLogger("cdisc.collect")


@dataclass
class RawItem:
    source: str                 # e.g. "reddit", "github", "rss:krebsonsecurity", "manual"
    source_ref: str             # stable id at the source (post id, repo full name, entry link)
    title: str = ""
    body: str = ""
    url: str | None = None
    author: str | None = None
    published_at: str | None = None   # ISO-8601 UTC
    generation: int = 0
    trusted: bool = False       # skip relevance gate (curated feeds, manual input)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return hashlib.sha256(f"{self.source}|{self.source_ref}".encode()).hexdigest()[:32]

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()

    def persist_raw(self) -> str | None:
        """Write the original payload to the evidence store; return its path."""
        if not self.raw:
            return None
        d = config.raw_store() / self.source.split(":")[0]
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{self.id}.json"
        if not p.exists():
            p.write_text(json.dumps(self.raw, default=str, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(p)

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("raw")
        row.pop("trusted")
        row["id"] = self.id
        row["raw_path"] = self.persist_raw()
        return row


class Collector:
    """Base class. Subclasses set ``name`` and implement ``collect``."""

    name: str = "base"
    kind: str = "generic"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg or {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def collect(self, since: datetime | None = None) -> Iterator[RawItem]:  # pragma: no cover
        raise NotImplementedError

    def search(self, term: str, since: datetime | None = None) -> Iterator[RawItem]:
        """Optional: run an ad-hoc query (used by the pivot engine)."""
        return iter(())


def iso(ts: float | int | str | None) -> str | None:
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(microsecond=0).isoformat()
    try:
        if isinstance(ts, str) and ts.isdigit():
            return iso(int(ts))
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except (ValueError, AttributeError):
        return None


def truncate(s: str | None, n: int = 20000) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "\n…[truncated]"
