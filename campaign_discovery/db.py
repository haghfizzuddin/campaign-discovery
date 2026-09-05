"""SQLite storage: the case database and lightweight knowledge graph.

Theme-neutral: the profile decides what the words mean.

Tables
------
sources        collectors and their last run
raw_items      everything collected, before relevance filtering (evidence store)
cases          relevant raw items promoted to cases
observables    IOCs and other atoms (domain, url, email, telegram, github_repo,
               wallet_*, sha256, brand, phrase, ip, nameserver, dependency)
case_observables  case -> observable edges with a role
relationships  typed edges between any two nodes (the knowledge graph)
search_terms   auto-generated pivots and their execution state
clusters / cluster_cases   output of the clustering step
enrichment     provider results per observable
cases_fts      FTS5 index over case title/body
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS counters (
    prefix TEXT PRIMARY KEY,
    value  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sources (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    last_run    TEXT,
    last_status TEXT,
    items_total INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS raw_items (
    id           TEXT PRIMARY KEY,       -- sha256(source|source_ref)
    source       TEXT NOT NULL,
    source_ref   TEXT NOT NULL,
    url          TEXT,
    author       TEXT,
    title        TEXT,
    body         TEXT,
    published_at TEXT,
    fetched_at   TEXT NOT NULL,
    relevance    REAL,
    processed    INTEGER NOT NULL DEFAULT 0,
    case_id      TEXT,
    generation   INTEGER NOT NULL DEFAULT 0,   -- 0 = seed collection, n = pivot depth
    raw_path     TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_processed ON raw_items(processed);
CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_items(source);

CREATE TABLE IF NOT EXISTS cases (
    id           TEXT PRIMARY KEY,       -- CASE-0001
    raw_item_id  TEXT NOT NULL REFERENCES raw_items(id),
    source       TEXT NOT NULL,
    url          TEXT,
    author       TEXT,
    title        TEXT,
    body         TEXT,
    published_at TEXT,
    created_at   TEXT NOT NULL,
    relevance    REAL NOT NULL,
    lure_type    TEXT,
    stage        TEXT,
    lure_text    TEXT,                   -- newline-joined candidate lure sentences
    status       TEXT NOT NULL DEFAULT 'new',   -- new | triaged | investigating | closed | false_positive
    notes        TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_published ON cases(published_at);

CREATE VIRTUAL TABLE IF NOT EXISTS cases_fts USING fts5(
    case_id UNINDEXED, title, body, lure_text, tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS observables (
    id         TEXT PRIMARY KEY,         -- OBS-000001
    type       TEXT NOT NULL,
    value      TEXT NOT NULL,            -- normalised (lowercase, refanged)
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    case_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(type, value)
);
CREATE INDEX IF NOT EXISTS idx_obs_type ON observables(type);

CREATE TABLE IF NOT EXISTS case_observables (
    case_id       TEXT NOT NULL REFERENCES cases(id),
    observable_id TEXT NOT NULL REFERENCES observables(id),
    role          TEXT NOT NULL,         -- links-to | contact | delivers | impersonates | pays-to | hash | mentions | phrase
    confidence    REAL NOT NULL DEFAULT 1.0,
    context       TEXT,                  -- short snippet around the match
    PRIMARY KEY (case_id, observable_id, role)
);
CREATE INDEX IF NOT EXISTS idx_co_obs ON case_observables(observable_id);

CREATE TABLE IF NOT EXISTS relationships (
    src_type   TEXT NOT NULL,
    src_id     TEXT NOT NULL,
    predicate  TEXT NOT NULL,
    dst_type   TEXT NOT NULL,
    dst_id     TEXT NOT NULL,
    case_id    TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (src_type, src_id, predicate, dst_type, dst_id)
);

CREATE TABLE IF NOT EXISTS search_terms (
    id          TEXT PRIMARY KEY,        -- Q-000001
    term        TEXT NOT NULL,
    kind        TEXT NOT NULL,           -- reddit | github | web | mastodon
    origin_case TEXT,
    generation  INTEGER NOT NULL DEFAULT 1,
    priority    INTEGER NOT NULL DEFAULT 3,   -- 1 = run first
    created_at  TEXT NOT NULL,
    last_run    TEXT,
    hits        INTEGER,
    new_items   INTEGER,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | done | manual | error
    UNIQUE(term, kind)
);

CREATE TABLE IF NOT EXISTS clusters (
    id          TEXT PRIMARY KEY,        -- CLU-0001
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    size        INTEGER NOT NULL,
    confidence  TEXT NOT NULL,           -- possible | probable | strongly_clustered (engine-derived)
    label       TEXT,
    first_seen  TEXT,
    summary     TEXT,                    -- JSON blob rendered by report.py
    status      TEXT NOT NULL DEFAULT 'new',   -- new | seen | investigating | closed
    is_new      INTEGER NOT NULL DEFAULT 1,    -- changed since the last report
    llm_notes   TEXT,
    analyst_assessment TEXT              -- JSON {lineage: same|different|unknown, note, assessed_at}; never engine-derived
);

CREATE TABLE IF NOT EXISTS cluster_cases (
    cluster_id TEXT NOT NULL REFERENCES clusters(id),
    case_id    TEXT NOT NULL REFERENCES cases(id),
    PRIMARY KEY (cluster_id, case_id)
);

CREATE TABLE IF NOT EXISTS enrichment (
    observable_id TEXT NOT NULL REFERENCES observables(id),
    provider      TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    ok            INTEGER NOT NULL DEFAULT 1,
    result        TEXT,                  -- JSON
    PRIMARY KEY (observable_id, provider)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(search_terms)")}
    if "priority" not in cols:
        conn.execute("ALTER TABLE search_terms ADD COLUMN priority INTEGER NOT NULL DEFAULT 3")
    ccols = {r["name"] for r in conn.execute("PRAGMA table_info(clusters)")}
    if "analyst_assessment" not in ccols:
        conn.execute("ALTER TABLE clusters ADD COLUMN analyst_assessment TEXT")
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def next_id(conn: sqlite3.Connection, prefix: str, width: int = 4) -> str:
    conn.execute(
        "INSERT INTO counters(prefix, value) VALUES (?, 1) "
        "ON CONFLICT(prefix) DO UPDATE SET value = value + 1",
        (prefix,),
    )
    val = conn.execute("SELECT value FROM counters WHERE prefix = ?", (prefix,)).fetchone()[0]
    return f"{prefix}-{val:0{width}d}"


# ---------------------------------------------------------------- raw items

def insert_raw(conn: sqlite3.Connection, item: dict[str, Any]) -> bool:
    """Insert a raw item; return False if it already existed."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_items(id, source, source_ref, url, author, title, body, "
        "published_at, fetched_at, generation, raw_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            item["id"], item["source"], item["source_ref"], item.get("url"), item.get("author"),
            item.get("title"), item.get("body"), item.get("published_at"), now_iso(),
            item.get("generation", 0), item.get("raw_path"),
        ),
    )
    return cur.rowcount == 1


def unprocessed_raw(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM raw_items WHERE processed = 0 ORDER BY fetched_at"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q).fetchall()


# ---------------------------------------------------------------- observables

def upsert_observable(conn: sqlite3.Connection, otype: str, value: str) -> str:
    row = conn.execute(
        "SELECT id FROM observables WHERE type = ? AND value = ?", (otype, value)
    ).fetchone()
    ts = now_iso()
    if row:
        conn.execute("UPDATE observables SET last_seen = ? WHERE id = ?", (ts, row["id"]))
        return row["id"]
    oid = next_id(conn, "OBS", 6)
    conn.execute(
        "INSERT INTO observables(id, type, value, first_seen, last_seen) VALUES (?,?,?,?,?)",
        (oid, otype, value, ts, ts),
    )
    return oid


def link_case_observable(
    conn: sqlite3.Connection, case_id: str, obs_id: str, role: str,
    confidence: float = 1.0, context: str | None = None,
) -> None:
    cur = conn.execute(
        "INSERT OR IGNORE INTO case_observables(case_id, observable_id, role, confidence, context) "
        "VALUES (?,?,?,?,?)",
        (case_id, obs_id, role, confidence, context),
    )
    if cur.rowcount == 1:
        conn.execute(
            "UPDATE observables SET case_count = "
            "(SELECT COUNT(DISTINCT case_id) FROM case_observables WHERE observable_id = ?) "
            "WHERE id = ?",
            (obs_id, obs_id),
        )


def add_relationship(
    conn: sqlite3.Connection, src_type: str, src_id: str, predicate: str,
    dst_type: str, dst_id: str, case_id: str | None = None, confidence: float = 1.0,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO relationships(src_type, src_id, predicate, dst_type, dst_id, "
        "case_id, confidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (src_type, src_id, predicate, dst_type, dst_id, case_id, confidence, now_iso()),
    )


# ---------------------------------------------------------------- enrichment

def save_enrichment(conn: sqlite3.Connection, obs_id: str, provider: str, result: Any, ok: bool = True) -> None:
    conn.execute(
        "INSERT INTO enrichment(observable_id, provider, fetched_at, ok, result) VALUES (?,?,?,?,?) "
        "ON CONFLICT(observable_id, provider) DO UPDATE SET fetched_at=excluded.fetched_at, "
        "ok=excluded.ok, result=excluded.result",
        (obs_id, provider, now_iso(), int(ok), json.dumps(result, default=str)),
    )


def get_enrichment(conn: sqlite3.Connection, obs_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in conn.execute("SELECT provider, result FROM enrichment WHERE observable_id = ?", (obs_id,)):
        try:
            out[row["provider"]] = json.loads(row["result"]) if row["result"] else None
        except json.JSONDecodeError:
            out[row["provider"]] = row["result"]
    return out


# ---------------------------------------------------------------- misc

def record_source_run(conn: sqlite3.Connection, name: str, kind: str, status: str, new_items: int) -> None:
    conn.execute(
        "INSERT INTO sources(name, kind, last_run, last_status, items_total) VALUES (?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run, last_status=excluded.last_status, "
        "items_total = items_total + excluded.items_total",
        (name, kind, now_iso(), status, new_items),
    )


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def one(q: str) -> Any:
        return conn.execute(q).fetchone()[0]

    return {
        "raw_items": one("SELECT COUNT(*) FROM raw_items"),
        "raw_unprocessed": one("SELECT COUNT(*) FROM raw_items WHERE processed = 0"),
        "cases": one("SELECT COUNT(*) FROM cases"),
        "observables": one("SELECT COUNT(*) FROM observables"),
        "relationships": one("SELECT COUNT(*) FROM relationships"),
        "clusters": one("SELECT COUNT(*) FROM clusters"),
        "clusters_new": one("SELECT COUNT(*) FROM clusters WHERE is_new = 1"),
        "search_terms_pending": one("SELECT COUNT(*) FROM search_terms WHERE status = 'pending'"),
        "observables_by_type": {
            r["type"]: r["n"]
            for r in conn.execute("SELECT type, COUNT(*) n FROM observables GROUP BY type ORDER BY n DESC")
        },
        "cases_by_source": {
            r["source"]: r["n"]
            for r in conn.execute("SELECT source, COUNT(*) n FROM cases GROUP BY source ORDER BY n DESC")
        },
    }
