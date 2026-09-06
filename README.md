# campaign-discovery

A research engine that turns public reports of scam and fraud activity into candidate campaign relationships, with the evidence for every link spelled out.

It collects what victims, researchers and feeds publish, extracts indicators and lure wording, enriches them, groups reports that share infrastructure or scripts, and produces a triage note per candidate cluster: what is shared, how specific each shared feature is, how confident the grouping is, and where to look next. The analyst makes the call on what it means.

The engine is theme-neutral. What counts as relevant, which brands get impersonated, which phrases are lures and which sources to read all live in a **profile**. The first profile is **recruitment-threat**: fake recruiters, malicious coding assessments, task scams, advance-fee job offers and DPRK-style fake interviews.

## The research problem

Recruitment-themed fraud and malware delivery is reported constantly, in fragments, across Reddit threads, Mastodon posts, GitHub repositories, vendor blogs and abuse feeds. Each fragment is one victim's view. The questions a researcher actually has are cross-fragment: which reports describe the same playbook, which infrastructure appears in more than one, which handles and wallets recur, and which of today's reports are worth an afternoon. Answering that by hand does not scale past a few dozen reports. Answering it with a black-box similarity score is not research. This engine automates the triage and keeps the reasoning inspectable.

## What a result looks like

```text
NEW CANDIDATE CLUSTER  CLU-0001  [possible]
Label: coding assessment malware · quick-load.vercel.app

Cases: 3
First seen: 2026-08-07
Sources: urlhaus (3)

Common lure:
  "tags contagious-interview curl-bash dropper fake-recruiter folderopen tasks-json vscode ua-curl vercel"

Shared infrastructure:
  - quick-load[.]vercel[.]app
  - 216[.]198[.]79[.]3
  - 64[.]29[.]17[.]3

Confidence: possible  (0 independent high-specificity, 2 medium, 0 low; capped: single reporter)

Evidence:
  [medium] domain              quick-load[.]vercel[.]app             CASE-0001, CASE-0002, CASE-0003
  [medium] wording_similarity  3 similar pair(s), max jaccard 0.73   CASE-0001, CASE-0002, CASE-0003
  [low   ] ip                  216.198.79.3                          CASE-0001, CASE-0002, CASE-0003
  [low   ] ip                  64.29.17.3                            CASE-0001, CASE-0002, CASE-0003

Analyst assessment: same_operational_lineage = unknown

Why this cluster exists:
  - 3/3 reports share recruiter/lure wording
  - 3/3 reference the same infrastructure or contact
  - 3 cases share domain quick-load.vercel.app
  - all posts by the same account (cross-post or repeat report)

Recommended pivots:
  -> Resolve sibling domains (shared IP / nameserver / registrar) for: quick-load.vercel.app
  -> Search phrase across archive: "tags contagious-interview curl-bash dropper fake-recruiter ..."
```

That is a real result from the first run: a Contagious Interview dropper on Vercel with mac, linux and windows payload paths. Note what the ladder did with it. Three URLs on one host, submitted by one URLhaus reporter, is one observation of one piece of infrastructure, so the engine says `possible` and explains the cap. The two Vercel edge IPs are low-specificity and grouped under the domain. Whether this is the same operator as anything else is left to the analyst.

Below the clusters, the report lists single cases with a strong signal of their own: domains registered in the last 90 days, messaging handles, repositories or accounts younger than two months, wallets.

## Architecture

```text
                 ENGINE (knows how to discover)                PROFILE (knows what matters)
                 ─────────────────────────────────              ───────────────────────────
 Reddit (Arctic Shift) ─┐                                       sources.yaml
 GitHub search ─────────┤                                         subreddits, queries, feeds,
 RSS / blogs ───────────┼─ collect ─► raw evidence store          tags, URLhaus filters,
 Mastodon tags ─────────┤                 │                       thresholds, specificity map
 URLhaus (filtered) ────┤                 ▼
 Manual URL / text ─────┘          relevance gate ◄──────────── lexicon.yaml
                                          │                       topic terms, harm terms,
                                          ▼                       campaign names, lure rules,
                                   extract ─► cases, observables, brands + cues, pivot
                                          │   relationships       templates, report labels
                          ┌───────────────┴───────────────┐
                          ▼                               ▼
                    enrichment                       pivot engine ──► new raw items
                    DNS · RDAP · GitHub deps          (prioritised, depth-limited)
                    abuse.ch · IntelOwl
                          │
                          ▼
                    clustering ─► candidate clusters
                          │        + structured evidence
                          ▼        + confidence ladder
                    report  ─► analyst review ─► `cdisc assess`
                          │
                          ▼ optional
                    Claude: cohesion check, narrative, questions (never attribution)
```

Deterministic code does collection, regex extraction, DNS and RDAP, hashing, dedup and graph construction. Reasoning about a finished evidence package is an optional Claude step you invoke per cluster.

## Evidence and confidence

Every cluster stores a list of evidence items: feature, value, specificity (high, medium, low, set per feature type in the profile), the cases that share it, and an independence group so that a URL, its domain, the IPs it resolves to and its nameservers count as one observation. Confidence is derived only from how many independent high-specificity groups stack:

| Level | Rule |
|---|---|
| `strongly_clustered` | two or more independent high-specificity groups, or one high plus two or more medium |
| `probable` | one high group, or two or more independent medium groups |
| `possible` | anything else that still formed a cluster |

The ladder stops there. Operator lineage is a separate field an analyst sets with `cdisc assess`, and the report shows both side by side. Shared infrastructure, shared code or shared wording alone do not prove operator identity, so the engine never says so. Details in [docs/evidence-model.md](docs/evidence-model.md); the working method in [docs/methodology.md](docs/methodology.md); a day-to-day walkthrough in [docs/usage.md](docs/usage.md).

## Install

```bash
git clone https://github.com/haghfizzuddin/campaign-discovery.git
cd campaign-discovery
pip install -e .                 # requests, feedparser, dnspython, networkx, PyYAML
pip install -e ".[llm]"          # optional: anthropic, for `cdisc interpret`
pip install -e ".[embeddings]"   # optional: sentence-transformers for paraphrase-tolerant similarity
pip install -e ".[dev]"          # pytest

cdisc init                       # ./data with an editable copy of the recruitment-threat profile
```

No API keys are required. Optional ones (see `.env.example`):

| Key | Unlocks |
|---|---|
| `ABUSECH_AUTH_KEY` | URLhaus host/URL and ThreatFox lookups (free key from auth.abuse.ch) |
| `GITHUB_TOKEN` | Higher GitHub search limits and README/dependency fetches at scale |
| `INTELOWL_URL` + `INTELOWL_API_KEY` | IntelOwl analyzers as an enrichment provider |
| `ANTHROPIC_API_KEY` | `cdisc interpret CLU-xxxx` |

## Usage

```bash
cdisc run --days 2 --ack            # the scheduled cycle; writes reports/clusters-<timestamp>.md
cdisc report                        # clusters with evidence, then leads outside clusters
cdisc report --new --min-confidence probable
cdisc assess CLU-0001 --lineage same --note "shared handle + payload host + identical script"
cdisc case CASE-0013                # one case: observables, cluster, generated queries
cdisc search '"coding challenge" telegram'
cdisc add-url https://www.linkedin.com/posts/...   # platforms that block automation
cdisc add-text -t "WhatsApp recruiter" < transcript.txt
cdisc pivot --list                  # queue of generated queries; reddit/github run automatically
cdisc interpret CLU-0001            # optional Claude review, stored on the cluster
cdisc profiles                      # list profiles; select with --profile or CDISC_PROFILE
```

Individual stages run on their own: `collect`, `process`, `enrich`, `pivot`, `cluster`, `report`.

Tuning loop: edit `data/profiles/recruitment-threat/lexicon.yaml`, then `cdisc reprocess` re-scores and re-extracts every stored item without collecting again (enrichment results are kept and re-attached), and `cdisc pivot --regenerate` rebuilds the query queue.

Scheduling is a cron line:

```cron
0 */6 * * *  cd /path/to/campaign-discovery && cdisc run --days 2 --ack >> data/run.log 2>&1
```

## First live run (recruitment-threat, 2026-09-06)

| Stage | Result |
|---|---|
| Collected | 877 items: r/Scams firehose, five job subreddits, GitHub, 8 security feeds, Mastodon, URLhaus |
| Passed the gate | 127 cases; 6 cross-posts folded into their originals |
| Extracted | 605 observables, 705 graph edges |
| Enriched | 83 domains (DNS + RDAP), 15 GitHub repos and accounts, no API keys |
| Pivot loop | one generated phrase query surfaced 12 older Reddit posts, 5 became cases |
| Clusters | 1 candidate (`possible`): a Contagious Interview dropper on Vercel with mac/linux/windows payload paths, one host, one reporter |
| Leads | 3 domains under 90 days old, 4 Telegram handles, 6 repositories under 30 days old, 1 wallet |

The relevance gate kept 24 of 500 r/Scams posts, all but two of them recruitment-related.

## How the pieces work

**Relevance gate.** Each item is scored from the profile's topic terms, harm terms and campaign names. A post needs both a topic and a harm context, or a campaign name. Title terms count double. The venue adds implicit weight (a r/Scams post is about fraud even when it never says "scam"). One stray harm word in an on-topic forum is dampened. News feeds face a higher bar than victim forums. Curated inputs (URLhaus rows matching campaign tags, anything added manually) bypass the gate.

**Extraction.** Regexes over refanged text (`hxxps://`, `[.]`, `[at]`). Bare domains are checked against a TLD allow-list and an ignore-list of platform, news and vendor domains; a report's own source domain is ignored. Messaging handles are only taken when the post mentions the platform. Lure phrases are quoted strings and keyword-dense sentences, normalised so identical scripts hash identically. Brands must appear with canonical capitalisation near a profile cue.

**Duplicates.** The same account posting the same report twice within a week is one case; the extra URL is kept in the case notes.

**Enrichment.** DNS A/NS/MX, RDAP with a newly-registered flag, abuse.ch, GitHub repo and owner metadata plus `package.json` / `requirements.txt` dependencies, IntelOwl. Resolved IPs, nameservers, registrars and uncommon dependencies become observables so infrastructure a victim never mentioned can still connect reports. Results are cached and survive a reprocess.

**Clustering.** Cases are nodes; shared observables and lure-wording similarity (word-bigram Jaccard with MinHash shortlisting; sentence-transformers optional) are edges. Observables shared by more than 30 percent of all cases are hubs and ignored. Low-specificity features link only when two kinds agree. Connected components become clusters with the evidence list above.

**Pivot loop.** Each new case generates prioritised, capped search queries: distinctive windows of its lure sentences, handles, domains, emails and brands. Cases that came from articles contribute wording only, never the domains they cite. Queries run against Reddit and GitHub on the next cycle; web queries are listed for you.

## Data model

SQLite with FTS5 (`data/cdisc.db`); raw payloads also as JSON under `data/raw/<source>/`.

```text
raw_items ─► cases ─┬─► case_observables ─► observables (domain, url, ip, email, telegram,
                    │                                    github_repo, github_user, wallet_*, hashes,
                    │                                    brand, phrase, nameserver, dependency, registrar)
                    ├─► relationships   (impersonates, uses-telegram, hosted-on, resolves-to,
                    │                    depends-on, duplicate-of, ...)
                    ├─► search_terms    (generated pivots: kind, priority, generation, run status)
                    └─► cluster_cases ─► clusters (summary JSON with evidence list, confidence,
                                                   analyst_assessment, llm_notes)
enrichment  (provider results per observable, keyed so they survive reprocessing)
```

The relationships table maps directly onto OpenCTI or MISP relationship objects when the corpus outgrows SQLite.

## Adding a profile

Copy `campaign_discovery/profiles/recruitment-threat/` to a new directory, rewrite the topic terms, harm terms, lure rules, brands, templates and labels for the new theme, point `sources.yaml` at the venues where that theme is discussed, and run with `--profile <name>`. The engine code does not change. Following the rule that a feature enters the engine only when two real cases need it, there are no parser or detection frameworks here yet.

## Source notes (2026)

- **Reddit** is read through [Arctic Shift](https://arctic-shift.photon-reddit.com), which archives Reddit and offers a free search API; public Reddit API automation is increasingly restricted. Full-text queries must be scoped to a subreddit or author, are expensive on their side, and answer `422 "Timeout. Maybe slow down a bit"` when pushed. The collector reads r/Scams as a paginated firehose, keeps the query matrix small, spaces requests and backs off.
- **abuse.ch** URLhaus and ThreatFox lookups need a free Auth-Key since 2025; the CSV feed does not.
- **Mastodon** public hashtag timelines only; instances that require auth for them (infosec.exchange) are skipped.
- **GitHub** unauthenticated REST is 60 requests/hour; README and dependency fetches are budgeted accordingly.
- **LinkedIn, X, Threads, WhatsApp** are not automated. Use `add-url` / `add-text`.

## Tests

```bash
pytest
```

Offline: extraction, similarity, evidence ladder, duplicate folding, reprocess-with-enrichment, and a full manual-report to cluster to report run.

## Roadmap

Driven by research cases, not by architecture. Candidates once a second case needs them: LSH banding for similarity beyond a few thousand cases, Arctic Shift comments, certificate-transparency sibling discovery, MISP/OpenCTI export, a second profile.

## License

MIT
