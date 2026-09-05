from campaign_discovery.cluster.similarity import estimate_jaccard, jaccard, minhash, shingles, similar_pairs


def test_shingles_ignore_stopwords():
    s = shingles("before we can schedule your technical interview")
    assert "schedule technical" in s and "before we" not in s


def test_minhash_tracks_jaccard():
    a = shingles("before we can schedule your technical interview please complete the coding challenge")
    b = shingles("before we can schedule your technical interview please complete the coding challenge and reply")
    exact = jaccard(a, b)
    est = estimate_jaccard(minhash(a), minhash(b))
    assert abs(exact - est) < 0.2 and exact > 0.6


def test_similar_pairs_finds_copied_script_only():
    texts = {
        "A": "before we can schedule your technical interview please complete the coding challenge",
        "B": "before scheduling the technical interview you need to complete this coding challenge first",
        "C": "my package got lost in the mail and the seller refuses to refund",
        "D": "before we can schedule your technical interview please complete the coding challenge and send it back",
    }
    pairs = {(a, b) for a, b, _ in similar_pairs(texts, 0.3)}
    assert ("A", "D") in pairs and ("A", "B") in pairs
    assert not any("C" in p for p in pairs)
