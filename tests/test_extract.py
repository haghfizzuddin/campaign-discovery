from campaign_discovery.extract import (classify_lure, classify_stage, extract_brands, extract_iocs,
                             extract_phrases, refang, relevance_score, defang)


def test_refang_variants():
    assert refang("hxxps://evil[.]com/x") == "https://evil.com/x"
    assert refang("hr[at]corp(.)io") == "hr@corp.io"
    assert refang("evil[dot]com") == "evil.com"


def test_defang_roundtrip():
    assert defang("https://a.b.c/x") == "hxxps://a[.]b[.]c/x"


def test_extract_core_iocs():
    text = ("Recruiter sent hxxps://assessment-example[.]com/task, repo github.com/EvilCorp/react-assessment, "
            "telegram @kraken_hr and t.me/+abcDEF12345, mail hr[at]kraken-careers[.]com, "
            "0x52908400098527886E0F7030069857D2E4169EE7, bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq, "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855, 45.33.32.156:8080, "
            "see reddit.com/r/Scams and file.py and 192.168.1.1")
    r = extract_iocs(text, {"reddit.com"})
    assert r["url"] == {"https://assessment-example.com/task"}
    assert r["domain"] == {"assessment-example.com"}          # file.py and reddit.com excluded
    assert r["github_repo"] == {"evilcorp/react-assessment"}
    assert r["github_user"] == {"evilcorp"}
    assert r["telegram"] == {"kraken_hr", "abcdef12345"}
    assert r["email"] == {"hr@kraken-careers.com"}
    assert r["wallet_eth"] == {"0x52908400098527886e0f7030069857d2e4169ee7"}
    assert "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq" in r["wallet_btc"]
    assert r["sha256"] == {"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}
    assert r["ip"] == {"45.33.32.156"}                        # private IP dropped
    assert "md5" not in r and "sha1" not in r                 # no sub-hash fragments


def test_telegram_handles_need_context():
    assert "telegram" not in extract_iocs("ping @someone about the meeting")
    assert extract_iocs("reach me on telegram @some_recruiter")["telegram"] == {"some_recruiter"}


def test_relevance_gate(lexicon, sources_cfg):
    thr = float(sources_cfg["relevance"]["threshold"])
    scam = ("A recruiter from Kraken messaged me on LinkedIn. Before the technical interview they wanted me "
            "to complete a coding challenge from a repo; npm install drained my wallet. Total scam, moved to Telegram.")
    benign_job = "Got a job offer at Google after a great interview, salary is nice!"
    benign_sec = "Mozi botnet spreads via new exploit; malware analysis inside."
    assert relevance_score(scam, lexicon)[0] > 0.6
    assert relevance_score(benign_job, lexicon)[0] < thr
    assert relevance_score(benign_sec, lexicon)[0] < thr
    # campaign name alone carries a post
    assert relevance_score("New BeaverTail sample seen in the wild", lexicon)[0] >= thr
    # one weak fraud word in a job forum is not evidence
    venting = "My recruiter ghosted me after the interview, honestly this whole job hunt feels fake"
    assert relevance_score(venting, lexicon)[0] < thr


def test_phrases_and_classification(lexicon):
    text = ('They said "Before we can schedule your technical interview, please complete the coding challenge" '
            "and sent a Bitbucket repo. When I ran npm install my wallet got drained.")
    phrases = extract_phrases(text, lexicon["lure_keywords"])
    assert phrases[0].startswith("before we can schedule your technical interview")
    lure, scores = classify_lure(text, lexicon["lure_types"])
    assert lure == "coding_assessment_malware"
    stages = classify_stage(text, lexicon["stages"])
    assert "assessment" in stages and "monetisation" in stages


def test_companies(lexicon):
    text = "A recruiter from Kraken (actually posing as Coinbase HR) offered a job at Zorbtech Labs."
    c = extract_brands(text, lexicon["brands"], lexicon["brand_cue_patterns"], lexicon["brand_exclusions"], lexicon["brand_context_regex"])
    assert c["Kraken"] == 0.9 and c["Coinbase"] == 0.9
    assert c.get("Zorbtech Labs") == 0.5
    # product mentions and lowercase common words are not employers
    noise = "we had the interview on Google Meet; the recruiter shared a google doc and asked me to target Q3"
    assert extract_brands(noise, lexicon["brands"], [], lexicon["brand_exclusions"], lexicon["brand_context_regex"]) == {}


def test_venue_context_boost(lexicon, sources_cfg):
    thr = float(sources_cfg["relevance"]["threshold"])
    scams = lexicon["source_context"]["reddit:Scams"]

    def score(title, body, ctx=None):
        # the pipeline scores title+body as text and passes the title separately
        return relevance_score(f"{title}\n\n{body}", lexicon, ctx, title=title)[0]

    t, b = "Obsidian Capital Co job offer", "My wife received this job offer yesterday and got interviewed the same exact day"
    assert score(t, b) < thr <= score(t, b, scams)
    # title terms count double: a bare "is this job a scam?" caption post passes in r/Scams
    assert score("Is this a Scam Job?", "screenshot attached", scams) >= thr
    # ...while a dating-scam post in the same venue does not
    assert score("Facebook Dating Scammer Caught",
                 "He asked me to send money for his position abroad, cv attached, offer of marriage", scams) < thr
    # ...and one stray "scam" in a r/jobs vent does not
    jobs = lexicon["source_context"]["reddit:jobs"]
    assert score("How to actually find a job in this market?",
                 "Applied to 300 jobs, every recruiter ghosts me, the whole market feels like a scam", jobs) < thr
    assert score("Is this job posting a scam?",
                 "Recruiter emailed from gmail, wants a fee for training before the interview", jobs) >= thr
