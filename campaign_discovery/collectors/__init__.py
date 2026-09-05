"""Collector registry."""
from __future__ import annotations

from typing import Any

from .base import Collector, RawItem
from .github import GitHubCollector
from .mastodon import MastodonCollector
from .reddit_arctic import RedditArcticCollector
from .rss import RSSCollector
from .urlhaus import URLhausCollector

# Fast, cheap sources first; the two rate-limited search APIs last.
REGISTRY: dict[str, type[Collector]] = {
    "urlhaus": URLhausCollector,
    "rss": RSSCollector,
    "mastodon": MastodonCollector,
    "reddit": RedditArcticCollector,
    "github": GitHubCollector,
}


def build(sources_cfg: dict[str, Any], only: list[str] | None = None) -> list[Collector]:
    out = []
    for name, cls in REGISTRY.items():
        if only and name not in only:
            continue
        c = cls(sources_cfg.get(name, {}))
        if c.enabled:
            out.append(c)
    return out


__all__ = ["Collector", "RawItem", "REGISTRY", "build"]
