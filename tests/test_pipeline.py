"""Offline end-to-end: manual reports -> cases -> graph -> clusters -> report."""
import json

from campaign_discovery import cases, cluster, config, db, report
from campaign_discovery.collectors import manual
from campaign_discovery.pivot import generate_queries

R1 = ("Kraken recruiter scam: A recruiter from Kraken messaged me on LinkedIn about a Senior React role. "
      'They said "Before we can schedule your technical interview, please complete the coding challenge" '
      "and sent https://assessment-kraken-hr.com/task with a repo github.com/krak3n-hr/react-assessment. "
      "They wanted to continue on Telegram @kraken_talent_hr. When I ran npm install, my MetaMask wallet was drained.")
R2 = ("Got the exact same thing from someone claiming to be Coinbase talent. Script was word for word: "
      '"before we can schedule your technical interview, please complete the coding challenge". '
      "Telegram handle was @kraken_talent_hr too. Total scam, fake recruiter.")
R3 = ("Fake recruiter posing as Binance sent me hxxps://assessment-kraken-hr[.]com/onboarding and asked for a "
      "$250 training fee in USDT before onboarding. Wallet TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE. Obvious job scam.")
R4 = ("Task scam: an app where you rate hotels and earn commission, then they ask you to recharge USDT to unlock "
      "withdrawals. Recruiter came via WhatsApp. Different site: hotel-rating-tasks.top")
R5 = "Legit question: I got a job offer at Google after a great interview, should I negotiate salary?"


def _ingest(conn, texts, lex, thr):
    for i, t in enumerate(texts):
        item = manual.from_text(t, title=f"report {i + 1}", url=f"https://example.invalid/{i}")
        db.insert_raw(conn, item.to_row())
    conn.commit()
    return cases.process_pending(conn, lex, thr)


def test_end_to_end(home, lexicon, sources_cfg):
    conn = db.connect()
    thr = float(sources_cfg["relevance"]["threshold"])
    n, created = _ingest(conn, [R1, R2, R3, R4, R5], lexicon, thr)
    assert n == 5
    # manual source is trusted -> all 5 become cases even the benign one
    assert created == 5

    obs = {(r["type"], r["value"]) for r in conn.execute("SELECT type, value FROM observables")}
    assert ("domain", "assessment-kraken-hr.com") in obs
    assert ("telegram", "kraken_talent_hr") in obs
    assert ("github_repo", "krak3n-hr/react-assessment") in obs
    assert ("wallet_trx", "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE") in obs
    assert ("brand", "Kraken") in obs and ("brand", "Binance") in obs
    assert ("domain", "example.invalid") not in obs   # invalid TLD ignored

    st = cluster.run(conn, sources_cfg)
    assert st["total"] >= 1
    rows = conn.execute("SELECT * FROM clusters ORDER BY size DESC").fetchall()
    top = rows[0]
    members = {r["case_id"] for r in conn.execute("SELECT case_id FROM cluster_cases WHERE cluster_id=?", (top["id"],))}
    assert members == {"CASE-0001", "CASE-0002", "CASE-0003"}   # R4/R5 stay out
    assert top["confidence"] == "strongly_clustered"
    s = json.loads(top["summary"])
    assert "kraken_talent_hr" in s["shared_telegram"]
    assert "assessment-kraken-hr.com" in s["shared_domains"]
    assert any("wording" in r for r in s["reasons"])
    assert s["pivots"]
    # structured evidence: handle is high-specificity, domain medium, wording present
    ev = {(e["feature"], e["value"]): e for e in s["evidence"]}
    assert ev[("telegram", "kraken_talent_hr")]["specificity"] == "high"
    assert set(ev[("telegram", "kraken_talent_hr")]["cases"]) == {"CASE-0001", "CASE-0002"}
    assert ev[("domain", "assessment-kraken-hr.com")]["specificity"] == "medium"
    assert any(e["feature"] in ("wording_similarity", "phrase") for e in s["evidence"])  # copied script
    assert s["confidence_basis"].startswith("1 independent high-specificity")
    assert s["single_reporter"] is False
    # the engine never emits an attribution state
    assert top["confidence"] in ("possible", "probable", "strongly_clustered")
    assert top["analyst_assessment"] is None

    text = report.render(conn, fmt="text", new_only=True)
    assert "NEW CANDIDATE CLUSTER" in text
    assert "assessment-kraken-hr[.]com" in text                            # defanged
    assert "Evidence:" in text and "[high  ] telegram" in text
    assert "same_operational_lineage = unknown" in text
    # analyst records lineage separately; report shows it, confidence unchanged
    report.set_assessment(conn, top["id"], "same", "shared handle + host + script")
    after = report.render(conn, fmt="text", new_only=True)
    assert "same_operational_lineage = same" in after
    assert conn.execute("SELECT confidence FROM clusters WHERE id=?", (top["id"],)).fetchone()[0] == top["confidence"]
    # the task-scam singleton is surfaced as a lead through its wallet-free but distinct domain? no:
    # leads need a strong signal; R4 has none, so the section lists nothing for it
    assert "hotel-rating-tasks[.]top" not in text
    assert report.mark_seen(conn) == len(rows)
    assert "No candidate clusters" in report.render(conn, fmt="text", new_only=True)

    # re-clustering is stable: same cluster id, not duplicated
    cluster.run(conn, sources_cfg)
    assert conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == len(rows)

    # pivots were generated from the cases
    pending = conn.execute("SELECT COUNT(*) FROM search_terms WHERE status='pending'").fetchone()[0]
    assert pending > 0
    terms = {r["term"] for r in conn.execute("SELECT term FROM search_terms")}
    assert "@kraken_talent_hr" in terms
    assert any("coding challenge" in t for t in terms)


def test_generate_queries_shape(lexicon):
    q = generate_queries(["before we can schedule your technical interview please complete the coding challenge"],
                         {"telegram": ["x_hr"], "domain": ["evil.com"], "brand": ["Kraken"]}, lexicon)
    kinds = {k for k, _, _ in q}
    assert kinds == {"reddit", "github", "web"}
    pairs = {(k, t) for k, t, _ in q}
    assert ("reddit", "@x_hr") in pairs and ("github", '"evil.com"') in pairs
    assert all(len(t) <= 200 for _, t, _ in q)
    # handles run before brands
    prio = {t: p for _, t, p in q}
    assert prio["@x_hr"] < prio["Kraken recruiter scam"]
    # article sources only pivot on wording, never on cited domains
    art = generate_queries(["before we can schedule your technical interview please complete the coding challenge"],
                           {"domain": ["cited-news.com"], "email": ["x@y.com"]}, lexicon, source_class="rss")
    assert not any("cited-news.com" in t or "x@y.com" in t for _, t, _ in art)
    assert any("coding challenge" in t for _, t, _ in art)
    gov = generate_queries([], {"domain": ["abr.business.gov.au", "evil.top"]}, lexicon, source_class="reddit")
    assert not any("gov.au" in t for _, t, _ in gov) and any("evil.top" in t for _, t, _ in gov)


def test_relevance_gate_for_untrusted_source(home, lexicon, sources_cfg):
    conn = db.connect()
    from campaign_discovery.collectors.base import RawItem
    good = RawItem(source="rss:test", source_ref="1", title="Fake recruiter scam", body=R1)
    bad = RawItem(source="rss:test", source_ref="2", title="Salary question", body=R5)
    for it in (good, bad):
        db.insert_raw(conn, it.to_row())
    n, created = cases.process_pending(conn, lexicon, float(sources_cfg["relevance"]["threshold"]))
    assert (n, created) == (2, 1)
    rel = {r["source_ref"]: r["relevance"] for r in conn.execute("SELECT source_ref, relevance FROM raw_items")}
    assert rel["1"] > rel["2"]


def test_cross_posts_fold_into_one_case(home, lexicon, sources_cfg):
    conn = db.connect()
    from campaign_discovery.collectors.base import RawItem
    a = RawItem(source="reddit:jobs", source_ref="p1", title="Is this recruiter a scam?", body=R1,
                author="victim42", published_at="2026-09-01T10:00:00+00:00")
    b = RawItem(source="reddit:recruitinghell", source_ref="p2", title="Is this recruiter a scam?", body=R1,
                author="victim42", published_at="2026-09-01T11:00:00+00:00")
    other = RawItem(source="reddit:Scams", source_ref="p3", title="Is this recruiter a scam?", body=R2,
                    author="someone_else", published_at="2026-09-01T12:00:00+00:00")
    for it in (a, b, other):
        db.insert_raw(conn, it.to_row())
    n, created = cases.process_pending(conn, lexicon, float(sources_cfg["relevance"]["threshold"]))
    assert (n, created) == (3, 2)
    dup = conn.execute("SELECT case_id FROM raw_items WHERE source_ref = 'p2'").fetchone()[0]
    assert dup == "CASE-0001"
    assert "Also posted" in conn.execute("SELECT notes FROM cases WHERE id = 'CASE-0001'").fetchone()[0]


def test_leads_surface_telegram_and_wallets(home, lexicon, sources_cfg):
    conn = db.connect()
    _ingest(conn, [R3], lexicon, float(sources_cfg["relevance"]["threshold"]))
    items = report.leads(conn)
    kinds = {i["type"] for i in items}
    assert "wallet_trx" in kinds
    text = report.render(conn, fmt="text")
    assert "LEADS OUTSIDE CLUSTERS" in text and "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE" in text


def test_reprocess_keeps_enrichment(home, lexicon, sources_cfg):
    from campaign_discovery import enrich
    from campaign_discovery.enrich import dns_rdap
    conn = db.connect()
    _ingest(conn, [R1], lexicon, float(sources_cfg["relevance"]["threshold"]))
    obs = conn.execute("SELECT * FROM observables WHERE type='domain' AND value='assessment-kraken-hr.com'").fetchone()
    fake = {"a": ["203.0.113.7"], "aaaa": [], "ns": ["ns1.example-dns.net"], "mx": [], "cname": [], "resolves": True}
    dns_rdap.dns_apply(conn, obs, fake)
    db.save_enrichment(conn, obs["id"], "dns", fake)
    conn.commit()
    kept = enrich.snapshot(conn)
    assert kept and kept[0]["provider"] == "dns"
    # simulate `cdisc reprocess`
    for t in ("cluster_cases", "clusters", "enrichment", "case_observables", "relationships",
              "search_terms", "observables", "cases_fts", "cases"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("UPDATE raw_items SET processed = 0, case_id = NULL, relevance = NULL")
    conn.execute("DELETE FROM counters WHERE prefix IN ('CASE','OBS','Q','CLU')")
    conn.commit()
    cases.process_pending(conn, lexicon, float(sources_cfg["relevance"]["threshold"]))
    assert enrich.restore(conn, kept) == 1
    new_obs = conn.execute("SELECT id FROM observables WHERE type='domain' AND value='assessment-kraken-hr.com'").fetchone()
    assert db.get_enrichment(conn, new_obs["id"])["dns"]["a"] == ["203.0.113.7"]
    # derived IP re-linked to the case without any lookup
    assert conn.execute("SELECT COUNT(*) FROM observables WHERE type='ip' AND value='203.0.113.7'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM case_observables co JOIN observables o ON o.id=co.observable_id "
                        "WHERE o.type='ip'").fetchone()[0] == 1


def test_confidence_ladder_rules():
    from campaign_discovery.cluster.engine import confidence_from_evidence as conf

    def ev(feature, spec, group=None, value="x"):
        return {"feature": feature, "value": value, "specificity": spec, "cases": ["A", "B"], "group": group or value}

    assert conf([ev("telegram", "high", value="h1"), ev("email", "high", value="h2")])[0] == "strongly_clustered"
    assert conf([ev("telegram", "high", value="h1"), ev("domain", "medium", value="d1"),
                 ev("phrase", "medium", value="p1")])[0] == "strongly_clustered"
    assert conf([ev("telegram", "high", value="h1")])[0] == "probable"
    assert conf([ev("domain", "medium", value="d1"), ev("phrase", "medium", value="p1")])[0] == "probable"
    assert conf([ev("domain", "medium", value="d1")])[0] == "possible"
    # a URL and its domain are one independence group: still probable, not strongly clustered
    assert conf([ev("url", "high", group="evil.com", value="https://evil.com/x"),
                 ev("domain", "medium", group="evil.com", value="evil.com")])[0] == "probable"
    # single reporter caps the ladder
    level, basis = conf([ev("telegram", "high", value="h1"), ev("email", "high", value="h2")], single_reporter=True)
    assert level == "possible" and "single reporter" in basis


def test_profile_switch_and_missing_profile(home, monkeypatch):
    from campaign_discovery import config
    assert config.profile() == "recruitment-threat"
    assert "recruitment-threat" in config.available_profiles()
    assert config.lexicon()["name"] == "recruitment-threat"
    monkeypatch.setenv("CDISC_PROFILE", "does-not-exist")
    import pytest
    with pytest.raises(FileNotFoundError):
        config.lexicon()
