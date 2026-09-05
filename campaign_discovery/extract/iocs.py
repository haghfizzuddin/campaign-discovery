"""Regex-based indicator extraction.

All values are returned normalised (lowercased where case is irrelevant,
refanged, stripped of trailing punctuation). Result shape:

    {"url": {...}, "domain": {...}, "ip": {...}, "email": {...},
     "telegram": {...}, "github_repo": {...}, "github_user": {...},
     "wallet_btc": {...}, "wallet_eth": {...}, "wallet_trx": {...},
     "md5": {...}, "sha1": {...}, "sha256": {...}}
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from .defang import refang

# A pragmatic TLD allow-list for *bare* domains (URLs with a scheme are always
# accepted). Keeps "file.py" and "e.g." out without needing the full IANA list.
TLDS = set("""
com net org io co me app dev xyz top site online info biz us uk de fr nl ru cn in au ca br jp kr eu ch se no
fi dk pl es it pt tr za sg hk tw my id ph vn th ae il ir pk ng ke tech store shop cloud live work club vip pro
link click space website fun icu buzz cyou cfd sbs rest lol wtf one run ai gg tv cc ws to ly sh ms tk ml ga cf
gq pw su ua kz by cz sk hu ro bg gr at be ie lu lt lv ee is mx ar cl pe ve uy ec bo py nz digital agency
solutions services group global network center systems team careers jobs hr recruitment consulting finance
capital exchange money crypto zone world today news email support help page zip mov ltd inc llc company
studio design media software technology tools wiki blog art io gov edu mil int asia mobi name tel travel
xin wang top kim ooo bid loan win review party trade date stream download racing accountant science faith
cricket men monster quest bond cam pics boats life fit cyou hair skin makeup beauty best cool guru host
press live studio express cash gold market land estate homes house cards games game pro cf
""".split())

# Domains: label(.label)+.tld with optional port stripped later
_DOMAIN_RE = re.compile(
    r"(?<![\w@/.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])", re.I
)
_URL_RE = re.compile(r"\bhttps?://[^\s<>\"'`)\]}]+", re.I)
_IP_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?::\d{2,5})?(?![\d.])")
_EMAIL_RE = re.compile(r"[\w.+-]+@(?:[a-z0-9-]+\.)+[a-z]{2,24}", re.I)
_TME_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/)?(\+?[A-Za-z0-9_]{4,64})", re.I)
_AT_HANDLE_RE = re.compile(r"(?<![\w.@/])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
_GITHUB_REPO_RE = re.compile(
    r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9_.-]{1,100})(?![\w-])", re.I
)
_GITHUB_USER_RE = re.compile(r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/?(?![\w/-])", re.I)
_BTC_RE = re.compile(r"\b(bc1[ac-hj-np-z02-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b")
_ETH_RE = re.compile(r"\b(0x[a-fA-F0-9]{40})\b")
_TRX_RE = re.compile(r"\b(T[1-9A-HJ-NP-Za-km-z]{33})\b")
_MD5_RE = re.compile(r"\b([a-fA-F0-9]{32})\b")
_SHA1_RE = re.compile(r"\b([a-fA-F0-9]{40})\b")
_SHA256_RE = re.compile(r"\b([a-fA-F0-9]{64})\b")

_GITHUB_RESERVED = {
    "features", "topics", "trending", "marketplace", "explore", "login", "join", "settings",
    "about", "pricing", "search", "orgs", "sponsors", "collections", "events", "site", "security",
    "enterprise", "team", "customer-stories", "readme", "issues", "pulls", "notifications", "new",
    "apps", "blog", "contact", "codespaces", "copilot",
}
# Real TLDs that far more often appear as file extensions in bare text.
# Only affects scheme-less matches; https://x.zip is still accepted.
_AMBIGUOUS_TLDS = {"py", "js", "ts", "go", "rs", "md", "sh", "so", "zip", "mov", "pl", "ps", "min", "map", "cc", "am", "pm"}
_TELEGRAM_CONTEXT = re.compile(r"telegram|\bt\.me\b|\btg\b", re.I)
_SHORT_TLD_NOISE = {"e.g", "i.e", "etc", "vs", "js", "py", "ts", "go", "rs", "md", "txt", "json", "yaml",
                    "yml", "exe", "dll", "zip", "rar", "png", "jpg", "jpeg", "gif", "pdf", "doc", "docx",
                    "xls", "xlsx", "csv", "html", "css", "php", "sh", "bat", "ps1", "so", "min", "map"}


def _clean_url(u: str) -> str:
    u = u.rstrip(".,;:!?'\"")
    while u and u[-1] in ")]}" and u.count(u[-1]) > u.count({")": "(", "]": "[", "}": "{"}[u[-1]]):
        u = u[:-1]
    return u


def _host_of(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


def _is_ignored(domain: str, ignore: set[str]) -> bool:
    """True if the domain or any parent (down to the registrable suffix) is ignored."""
    parts = domain.split(".")
    return any(".".join(parts[i:]) in ignore for i in range(max(1, len(parts) - 1)))


def _valid_bare_domain(domain: str) -> bool:
    tld = domain.rsplit(".", 1)[-1].lower()
    if tld not in TLDS or tld in _AMBIGUOUS_TLDS:
        return False
    if domain.lower() in _SHORT_TLD_NOISE:
        return False
    labels = domain.split(".")
    if len(labels) > 6 or any(len(l) > 63 for l in labels):
        return False
    return True


def extract_iocs(text: str, ignore_domains: set[str] | None = None) -> dict[str, set[str]]:
    ignore = {d.lower() for d in (ignore_domains or set())}
    t = refang(text)
    out: dict[str, set[str]] = {k: set() for k in (
        "url", "domain", "ip", "email", "telegram", "github_repo", "github_user",
        "wallet_btc", "wallet_eth", "wallet_trx", "md5", "sha1", "sha256",
    )}

    # ---- URLs and their hosts
    for m in _URL_RE.finditer(t):
        url = _clean_url(m.group(0))
        host = _host_of(url)
        if not host:
            continue
        gh = _GITHUB_REPO_RE.search(url)
        if host in ("github.com", "www.github.com"):
            if gh and gh.group(1).lower() not in _GITHUB_RESERVED:
                repo = f"{gh.group(1)}/{gh.group(2).rstrip('.')}".lower().removesuffix(".git")
                out["github_repo"].add(repo)
                out["github_user"].add(gh.group(1).lower())
            continue
        if host in ("t.me", "telegram.me"):
            tm = _TME_RE.search(url)
            if tm:
                out["telegram"].add(tm.group(1).lower().lstrip("+"))
            continue
        try:
            ip = ipaddress.ip_address(host)
            if not (ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local or ip.is_multicast):
                out["ip"].add(host)
                out["url"].add(url)
            continue
        except ValueError:
            pass
        if "." not in host or _is_ignored(host, ignore):
            continue   # localhost, intranet names, platform domains
        out["url"].add(url)
        out["domain"].add(host)

    # ---- GitHub repos written without a scheme
    for m in _GITHUB_REPO_RE.finditer(t):
        owner = m.group(1).lower()
        if owner in _GITHUB_RESERVED:
            continue
        out["github_repo"].add(f"{owner}/{m.group(2).rstrip('.').lower().removesuffix('.git')}")
        out["github_user"].add(owner)
    for m in _GITHUB_USER_RE.finditer(t):
        owner = m.group(1).lower()
        if owner not in _GITHUB_RESERVED:
            out["github_user"].add(owner)

    # ---- Telegram
    for m in _TME_RE.finditer(t):
        out["telegram"].add(m.group(1).lower().lstrip("+"))
    if _TELEGRAM_CONTEXT.search(t):
        for m in _AT_HANDLE_RE.finditer(t):
            handle = m.group(1)
            # skip Mastodon-style @user@instance and emails (lookbehind already excludes)
            end = m.end()
            if end < len(t) and t[end] == "@":
                continue
            out["telegram"].add(handle.lower())

    # ---- Emails
    for m in _EMAIL_RE.finditer(t):
        email = m.group(0).lower().rstrip(".")
        out["email"].add(email)

    # ---- Bare domains
    email_hosts = {e.split("@", 1)[1] for e in out["email"]}
    for m in _DOMAIN_RE.finditer(t):
        d = m.group(1).lower().rstrip(".")
        if d in email_hosts or _is_ignored(d, ignore) or not _valid_bare_domain(d):
            continue
        if d in ("github.com", "t.me"):
            continue
        out["domain"].add(d)

    # ---- IPs
    for m in _IP_RE.finditer(t):
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast or ip.is_link_local:
            continue
        out["ip"].add(str(ip))

    # ---- Hashes (longest first so a sha256 is not also reported as md5 fragments)
    for m in _SHA256_RE.finditer(t):
        out["sha256"].add(m.group(1).lower())
    for m in _SHA1_RE.finditer(t):
        h = m.group(1).lower()
        if not any(h in s for s in out["sha256"]):
            out["sha1"].add(h)
    for m in _MD5_RE.finditer(t):
        h = m.group(1).lower()
        if not any(h in s for s in out["sha256"] | out["sha1"]):
            out["md5"].add(h)

    # ---- Wallets
    for m in _ETH_RE.finditer(t):
        out["wallet_eth"].add(m.group(1).lower())
    for m in _BTC_RE.finditer(t):
        v = m.group(1)
        if v.lower() not in out["sha1"] and not v.isdigit():
            out["wallet_btc"].add(v if v.startswith("bc1") else v)
    for m in _TRX_RE.finditer(t):
        out["wallet_trx"].add(m.group(1))

    return {k: v for k, v in out.items() if v}
