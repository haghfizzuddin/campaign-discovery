"""Refang / defang helpers for indicators written by humans in security posts."""
from __future__ import annotations

import re

_REFANG_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bhxxps?\b", re.I), lambda m: m.group(0).lower().replace("xx", "tt")),  # type: ignore[list-item]
    (re.compile(r"\[(?:\.|dot)\]", re.I), "."),
    (re.compile(r"\((?:\.|dot)\)", re.I), "."),
    (re.compile(r"\{(?:\.|dot)\}", re.I), "."),
    (re.compile(r"\s+\[?dot\]?\s+", re.I), "."),
    (re.compile(r"\[:\]"), ":"),
    (re.compile(r"\[://\]"), "://"),
    (re.compile(r":\\/\\/"), "://"),
    (re.compile(r"\[/\]"), "/"),
    (re.compile(r"\[(?:@|at)\]", re.I), "@"),
    (re.compile(r"\((?:@|at)\)", re.I), "@"),
    (re.compile(r"\s+\[?at\]?\s+(?=[\w-]+\.[a-z]{2,})", re.I), "@"),
]


def refang(text: str) -> str:
    """Turn defanged indicators back into their live form (hxxp -> http, [.] -> .)."""
    out = text
    for pattern, repl in _REFANG_RULES:
        out = pattern.sub(repl, out)  # type: ignore[arg-type]
    return out


def defang(value: str) -> str:
    """Render an indicator so it cannot be clicked: https://a.b -> hxxps://a[.]b"""
    v = value.replace("http://", "hxxp://").replace("https://", "hxxps://")
    return v.replace(".", "[.]")
