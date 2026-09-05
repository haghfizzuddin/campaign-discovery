"""cdisc — campaign-discovery command line.

    cdisc init                       create data dir + editable copy of the active profile
    cdisc run                        collect -> process -> enrich -> pivot -> cluster -> report
    cdisc collect [--source X]       run collectors only
    cdisc process                    score + extract pending raw items into cases
    cdisc reprocess                  redo extraction after tuning the profile (keeps enrichment)
    cdisc enrich [--provider ..]     DNS / RDAP / abuse.ch / GitHub / IntelOwl
    cdisc pivot [--list|--regenerate] execute or inspect auto-generated queries
    cdisc cluster                    rebuild candidate clusters + evidence
    cdisc report [--new] [--md]      triage notes: clusters, evidence, leads
    cdisc assess CLU-0001 --lineage same|different|unknown [--note ..]
    cdisc add-url URL                manual evidence (LinkedIn/X/Threads/blog)
    cdisc add-text [-t TITLE] < f    manual evidence from stdin
    cdisc case CASE-0001             show a case and its observables
    cdisc search "coding challenge"  full-text search across cases
    cdisc interpret CLU-0001         ask Claude to review a cluster (optional)
    cdisc profiles | stats | sources

Global: --profile NAME (default recruitment-threat; or CDISC_PROFILE).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cases, cluster, config, db, enrich, pivot, report
from .collectors import build as build_collectors
from .collectors import manual

log = logging.getLogger("cdisc")


def _since(days: float | None) -> datetime | None:
    return datetime.now(timezone.utc) - timedelta(days=days) if days else None


def cmd_init(a: argparse.Namespace) -> None:
    copied = config.init_home(force=a.force)
    conn = db.connect()
    conn.close()
    print(f"home:    {config.home()}")
    print(f"db:      {config.db_path()}")
    print(f"profile: {config.profile()}")
    for p in copied:
        print(f"wrote {p}")
    if not copied:
        print("config already present (use --force to overwrite)")


def cmd_collect(a: argparse.Namespace, conn=None) -> dict[str, int]:
    conn = conn or db.connect()
    cfg = config.sources()
    only = a.source or None
    totals: dict[str, int] = {}
    for c in build_collectors(cfg, only):
        new = seen = 0
        status = "ok"
        try:
            for item in c.collect(since=_since(a.days)):
                seen += 1
                if db.insert_raw(conn, item.to_row()):
                    new += 1
                # commit per item: collectors sleep between requests, and an open
                # write transaction would block `process`/`enrich` running alongside
                conn.commit()
        except Exception as exc:
            status = f"error: {exc}"
            log.exception("collector %s failed", c.name)
        db.record_source_run(conn, c.name, c.kind, status, new)
        conn.commit()
        totals[c.name] = new
        print(f"collect {c.name:<10} seen={seen:<5} new={new:<5} {status}")
    return totals


def cmd_process(a: argparse.Namespace, conn=None) -> tuple[int, int]:
    conn = conn or db.connect()
    lex = config.lexicon()
    thr = float(config.sources().get("relevance", {}).get("threshold", 0.35))
    if getattr(a, "threshold", None) is not None:
        thr = a.threshold
    trusted = config.sources().get("trusted_sources") or cases.DEFAULT_TRUSTED_SOURCES
    n, created = cases.process_pending(conn, lex, thr, limit=getattr(a, "limit", None), trusted_sources=trusted)
    print(f"process    raw={n} cases_created={created} threshold={thr}")
    return n, created


def cmd_reprocess(a: argparse.Namespace) -> None:
    """Drop derived data (cases, observables, clusters, queries) and re-run
    extraction over the stored raw items. Use after tuning the lexicon."""
    conn = db.connect()
    kept = enrich.snapshot(conn)
    with db.tx(conn):
        for t in ("cluster_cases", "clusters", "enrichment", "case_observables", "relationships",
                  "search_terms", "observables", "cases_fts", "cases"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("UPDATE raw_items SET processed = 0, case_id = NULL, relevance = NULL")
        conn.execute("DELETE FROM counters WHERE prefix IN ('CASE','OBS','Q','CLU')")
    print(f"derived tables cleared ({len(kept)} enrichment results kept)")
    cmd_process(a, conn)
    restored = enrich.restore(conn, kept)
    print(f"enrichment restored for {restored} observables (no network calls)")


def cmd_enrich(a: argparse.Namespace, conn=None) -> dict[str, int]:
    conn = conn or db.connect()
    st = enrich.run(conn, config.sources(), providers=a.provider or None, limit=getattr(a, "limit", None))
    print(f"enrich     {st}")
    return st


def cmd_pivot(a: argparse.Namespace, conn=None) -> dict[str, int]:
    conn = conn or db.connect()
    if getattr(a, "regenerate", False):
        n = pivot.regenerate(conn, config.lexicon())
        print(f"pivot      regenerated {n} queries from existing cases")
        return {"regenerated": n}
    if getattr(a, "list", False):
        for r in conn.execute("SELECT id, kind, generation, status, hits, term FROM search_terms "
                              "ORDER BY status, generation, created_at LIMIT ?", (a.limit or 200,)):
            print(f"{r['id']}  {r['kind']:<7} g{r['generation']} {r['status']:<8} hits={r['hits'] if r['hits'] is not None else '-':<4} {r['term']}")
        return {}
    st = pivot.run_pending(conn, config.sources(), limit=getattr(a, "limit", None))
    print(f"pivot      {st}")
    return st


def cmd_cluster(a: argparse.Namespace, conn=None) -> dict[str, int]:
    conn = conn or db.connect()
    st = cluster.run(conn, config.sources(), config.lexicon())
    print(f"cluster    {st}")
    return st


def cmd_report(a: argparse.Namespace, conn=None) -> str:
    conn = conn or db.connect()
    fmt = "json" if a.json else ("md" if a.md else "text")
    text = report.render(conn, fmt=fmt, new_only=a.new, min_confidence=a.min_confidence, limit=a.limit)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out}")
    else:
        print(text)
    if a.ack:
        n = report.mark_seen(conn)
        print(f"acknowledged {n} cluster(s)")
    return text


def cmd_run(a: argparse.Namespace) -> None:
    """The scheduled entry point: wake up, collect, triage, report."""
    conn = db.connect()
    cmd_collect(a, conn)
    cmd_process(a, conn)
    if not a.no_enrich:
        a.provider = None
        cmd_enrich(a, conn)
    if not a.no_pivot:
        a.limit = None
        cmd_pivot(a, conn)
        cmd_process(a, conn)          # cases discovered through pivots
        if not a.no_enrich:
            cmd_enrich(a, conn)
    cmd_cluster(a, conn)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    out = a.out or str(Path("reports") / f"clusters-{stamp}.md")
    a.json, a.md, a.new, a.min_confidence, a.limit, a.out, a.ack = False, True, True, None, None, out, a.ack
    cmd_report(a, conn)
    print()
    text = report.render(conn, fmt="text", new_only=True)
    print(text)


def cmd_add_url(a: argparse.Namespace) -> None:
    conn = db.connect()
    item = manual.from_url(a.url, note=a.note)
    new = db.insert_raw(conn, item.to_row())
    conn.commit()
    print(f"{'added' if new else 'already present'}: {item.title[:80]}")
    if new and not a.no_process:
        cmd_process(a, conn)


def cmd_add_text(a: argparse.Namespace) -> None:
    conn = db.connect()
    text = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
    if not text.strip():
        sys.exit("no text provided")
    item = manual.from_text(text, title=a.title, url=a.url, author=a.author, published_at=a.date)
    new = db.insert_raw(conn, item.to_row())
    conn.commit()
    print(f"{'added' if new else 'already present'}: {item.title[:80]}")
    if new and not a.no_process:
        cmd_process(a, conn)


def cmd_case(a: argparse.Namespace) -> None:
    conn = db.connect()
    b = cases.case_bundle(conn, a.case_id)
    if not b:
        sys.exit(f"unknown case {a.case_id}")
    if a.json:
        print(json.dumps(b, indent=1, ensure_ascii=False, default=str))
        return
    c = b["case"]
    print(f"{c['id']}  [{c['source']}]  relevance={c['relevance']}  lure={c['lure_type']}  stages={c['stage']}")
    print(f"title: {c['title']}\nurl:   {c['url']}\nauthor: {c['author']}  published: {c['published_at']}")
    print(f"clusters: {', '.join(b['clusters']) or '-'}\n")
    print("lure text:")
    for line in (c["lure_text"] or "").split("\n"):
        if line:
            print(f"  · {line}")
    print("\nobservables:")
    for o in b["observables"]:
        shared = f" (in {o['case_count']} cases)" if o["case_count"] > 1 else ""
        print(f"  {o['type']:<12} {o['role']:<12} {o['value']}{shared}")
    if b["search_terms"]:
        print("\ngenerated queries:")
        for t in b["search_terms"]:
            print(f"  {t['kind']:<7} {t['status']:<8} {t['term']}")
    if a.body:
        print("\n--- body ---\n" + (c["body"] or ""))


def cmd_search(a: argparse.Namespace) -> None:
    conn = db.connect()
    rows = conn.execute(
        "SELECT c.id, c.source, c.published_at, c.title, snippet(cases_fts, 2, '[', ']', '…', 12) AS snip "
        "FROM cases_fts JOIN cases c ON c.id = cases_fts.case_id WHERE cases_fts MATCH ? "
        "ORDER BY bm25(cases_fts) LIMIT ?", (a.query, a.limit)).fetchall()
    for r in rows:
        print(f"{r['id']}  {(r['published_at'] or '')[:10]:<10} {r['source']:<16} {(r['title'] or '')[:60]}")
        print(f"    {r['snip']}")
    if not rows:
        print("no matches")


def cmd_stats(a: argparse.Namespace) -> None:
    conn = db.connect()
    st = db.stats(conn)
    print(json.dumps(st, indent=1))
    print("\nsources:")
    for r in conn.execute("SELECT * FROM sources ORDER BY name"):
        print(f"  {r['name']:<10} last={r['last_run'] or '-':<26} total={r['items_total']:<6} {r['last_status']}")


def cmd_interpret(a: argparse.Namespace) -> None:
    from . import llm
    conn = db.connect()
    result = llm.interpret(conn, a.cluster_id, model=a.model)
    print(json.dumps(result, indent=1, ensure_ascii=False))


def cmd_assess(a: argparse.Namespace) -> None:
    conn = db.connect()
    rec = report.set_assessment(conn, a.cluster_id, a.lineage, a.note)
    print(f"{a.cluster_id}: same_operational_lineage = {rec['lineage']}" + (f" ({rec['note']})" if rec["note"] else ""))


def cmd_profiles(a: argparse.Namespace) -> None:
    active = config.profile()
    for name in config.available_profiles():
        mark = "*" if name == active else " "
        try:
            import os
            os.environ["CDISC_PROFILE"] = name
            desc = " ".join((config.lexicon().get("description") or "").split())
        finally:
            os.environ["CDISC_PROFILE"] = active
        print(f"{mark} {name:<24} {desc[:90]}")


def cmd_sources(a: argparse.Namespace) -> None:
    cfg = config.sources()
    for name, c in cfg.items():
        if name in ("pivot", "enrichment", "clustering", "relevance"):
            continue
        print(f"{name:<10} enabled={c.get('enabled', True)}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cdisc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--profile", help="profile to run with (default: recruitment-threat or $CDISC_PROFILE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create data dir and editable config"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_init)

    s = sub.add_parser("run", help="full pipeline (for cron)")
    s.add_argument("--days", type=float, default=None, help="only collect items newer than N days")
    s.add_argument("--source", action="append", help="restrict collectors")
    s.add_argument("--no-enrich", action="store_true"); s.add_argument("--no-pivot", action="store_true")
    s.add_argument("--out", help="markdown report path (default reports/clusters-<ts>.md)")
    s.add_argument("--ack", action="store_true", help="mark reported clusters as seen")
    s.set_defaults(fn=cmd_run, threshold=None, limit=None)

    s = sub.add_parser("collect"); s.add_argument("--source", action="append", choices=["reddit", "github", "rss", "mastodon", "urlhaus"])
    s.add_argument("--days", type=float, default=None); s.set_defaults(fn=cmd_collect)

    s = sub.add_parser("process"); s.add_argument("--threshold", type=float); s.add_argument("--limit", type=int); s.set_defaults(fn=cmd_process)

    s = sub.add_parser("reprocess", help="re-run extraction over stored raw items (after lexicon changes)")
    s.add_argument("--threshold", type=float); s.set_defaults(fn=cmd_reprocess, limit=None)

    s = sub.add_parser("enrich"); s.add_argument("--provider", action="append", choices=list(enrich.PROVIDERS))
    s.add_argument("--limit", type=int); s.set_defaults(fn=cmd_enrich)

    s = sub.add_parser("pivot"); s.add_argument("--limit", type=int); s.add_argument("--list", action="store_true")
    s.add_argument("--regenerate", action="store_true", help="rebuild unexecuted queries from all cases"); s.set_defaults(fn=cmd_pivot)

    s = sub.add_parser("cluster"); s.set_defaults(fn=cmd_cluster)

    s = sub.add_parser("report"); s.add_argument("--new", action="store_true", help="only clusters changed since last ack")
    s.add_argument("--md", action="store_true"); s.add_argument("--json", action="store_true")
    s.add_argument("--min-confidence", choices=["strongly_clustered", "probable", "possible"]); s.add_argument("--limit", type=int)
    s.add_argument("--out"); s.add_argument("--ack", action="store_true"); s.set_defaults(fn=cmd_report)

    s = sub.add_parser("add-url"); s.add_argument("url"); s.add_argument("--note"); s.add_argument("--no-process", action="store_true")
    s.set_defaults(fn=cmd_add_url, threshold=None, limit=None)

    s = sub.add_parser("add-text"); s.add_argument("-t", "--title"); s.add_argument("-f", "--file"); s.add_argument("--url")
    s.add_argument("--author"); s.add_argument("--date"); s.add_argument("--no-process", action="store_true")
    s.set_defaults(fn=cmd_add_text, threshold=None, limit=None)

    s = sub.add_parser("case"); s.add_argument("case_id"); s.add_argument("--json", action="store_true"); s.add_argument("--body", action="store_true"); s.set_defaults(fn=cmd_case)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--limit", type=int, default=20); s.set_defaults(fn=cmd_search)
    s = sub.add_parser("assess", help="record the analyst's lineage call for a cluster")
    s.add_argument("cluster_id"); s.add_argument("--lineage", required=True, choices=["same", "different", "unknown"])
    s.add_argument("--note"); s.set_defaults(fn=cmd_assess)
    s = sub.add_parser("profiles", help="list available profiles"); s.set_defaults(fn=cmd_profiles)
    s = sub.add_parser("stats"); s.set_defaults(fn=cmd_stats)
    s = sub.add_parser("sources"); s.set_defaults(fn=cmd_sources)
    s = sub.add_parser("interpret"); s.add_argument("cluster_id"); s.add_argument("--model", default="claude-opus-5"); s.set_defaults(fn=cmd_interpret)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.profile:
        import os
        os.environ["CDISC_PROFILE"] = args.profile
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S",
                        stream=sys.stderr)
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        for noisy in ("cdisc.cases", "cdisc.pivot", "cdisc.cluster"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    args.fn(args)


if __name__ == "__main__":
    main()
