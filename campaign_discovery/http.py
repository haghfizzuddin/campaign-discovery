"""Small shared HTTP helper: one session, sane UA, timeouts, retries, politeness."""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from . import __version__

log = logging.getLogger("cdisc.http")

UA = f"campaign-discovery/{__version__} (+https://github.com/haghfizzuddin/campaign-discovery; research)"
_session: requests.Session | None = None
_last_call: dict[str, float] = {}


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": UA, "Accept": "application/json, text/*;q=0.8, */*;q=0.5"})
    return _session


def get(url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
        timeout: float = 30, retries: int = 2, min_interval: float = 0.0, key: str | None = None,
        **kw: Any) -> requests.Response:
    return _request("GET", url, params=params, headers=headers, timeout=timeout, retries=retries,
                    min_interval=min_interval, key=key, **kw)


def post(url: str, *, data: Any = None, json: Any = None, headers: dict[str, str] | None = None,
         timeout: float = 30, retries: int = 2, min_interval: float = 0.0, key: str | None = None,
         **kw: Any) -> requests.Response:
    return _request("POST", url, data=data, json=json, headers=headers, timeout=timeout, retries=retries,
                    min_interval=min_interval, key=key, **kw)


def _request(method: str, url: str, *, retries: int, min_interval: float, key: str | None,
             **kw: Any) -> requests.Response:
    k = key or url.split("/")[2]
    if min_interval:
        wait = _last_call.get(k, 0) + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = session().request(method, url, **kw)
            _last_call[k] = time.monotonic()
            if resp.status_code in (429, 502, 503, 504) and attempt < retries:
                delay = float(resp.headers.get("Retry-After") or 2 ** attempt * 2)
                log.warning("%s %s -> %s, retrying in %.0fs", method, url, resp.status_code, delay)
                time.sleep(min(delay, 60))
                continue
            return resp
        except requests.RequestException as exc:  # network error
            last_exc = exc
            if attempt < retries:
                time.sleep(2 ** attempt)
    assert last_exc is not None
    raise last_exc


def html_to_text(html: str) -> str:
    """Dependency-free HTML -> text (good enough for feeds and pasted pages)."""
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
            self._skip = 0
            self._in_link = 0   # inside <a href=...>: emit the href, drop the display text
                                # (Mastodon renders links as "infosec.e…xchange/..." spans)

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag in ("script", "style", "noscript"):
                self._skip += 1
            elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "blockquote", "pre"):
                self.parts.append("\n")
            if tag == "a":
                a = dict(attrs)
                href = a.get("href")
                cls = a.get("class") or ""
                if "hashtag" in cls or "mention" in cls:
                    return  # "#scam" / "@user" read better as text than as a URL
                if href and href.startswith("http"):
                    self.parts.append(f" {href} ")
                    self._in_link += 1

        def handle_endtag(self, tag: str) -> None:
            if tag in ("script", "style", "noscript") and self._skip:
                self._skip -= 1
            if tag == "a" and self._in_link:
                self._in_link -= 1

        def handle_data(self, data: str) -> None:
            if not self._skip and not self._in_link:
                self.parts.append(data)

    p = _P()
    try:
        p.feed(html or "")
    except Exception:  # pragma: no cover - malformed HTML
        return html or ""
    text = "".join(p.parts)
    lines = [" ".join(l.split()) for l in text.splitlines()]
    return "\n".join(l for l in lines if l)
