"""Manual inputs: a URL to fetch, pasted text, or a saved file.

This is the escape hatch for platforms hostile to automation (LinkedIn, X,
Threads, WhatsApp screenshots you transcribe). Manual items are trusted.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from ..http import get, html_to_text
from .base import RawItem, truncate

log = logging.getLogger("cdisc.collect.manual")


def from_url(url: str, note: str | None = None) -> RawItem:
    resp = get(url, timeout=30, headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    ctype = resp.headers.get("Content-Type", "")
    text = html_to_text(resp.text) if "html" in ctype else resp.text
    title = ""
    for line in text.splitlines():
        if line.strip():
            title = line.strip()[:140]
            break
    body = text
    if note:
        body = f"Analyst note: {note}\n\n{body}"
    return RawItem(
        source="manual", source_ref=url, title=title or url, body=truncate(body, 40000), url=url,
        trusted=True, raw={"url": url, "status": resp.status_code, "content_type": ctype, "note": note},
    )


def from_text(text: str, title: str | None = None, url: str | None = None, author: str | None = None,
              published_at: str | None = None) -> RawItem:
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    return RawItem(
        source="manual", source_ref=url or f"text:{digest}", title=title or text.strip().split("\n", 1)[0][:140],
        body=truncate(text, 40000), url=url, author=author, published_at=published_at, trusted=True,
        raw={"text": text, "title": title, "url": url, "author": author},
    )


def from_file(path: str | Path, url: str | None = None) -> RawItem:
    p = Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace")
    text = html_to_text(raw) if p.suffix.lower() in (".html", ".htm") else raw
    return from_text(text, title=p.stem, url=url or f"file:{p.name}")
