# Using campaign-discovery

A day-to-day guide. Command examples show real output from the first live run
of the recruitment-threat profile (2026-09-06).

## The one command that matters

```bash
cdisc run --days 2 --ack
```

This is the scheduled cycle. It collects new items from every enabled source,
scores them through the relevance gate, extracts indicators and lure wording
into cases, enriches domains and repositories, executes ten pending pivot
queries, re-clusters, writes `reports/clusters-<timestamp>.md`, and prints the
clusters that changed since the last acknowledgement. Put it in cron every six
hours and read the report when it says something new:

```cron
0 */6 * * *  cd /path/to/campaign-discovery && cdisc run --days 2 --ack >> data/run.log 2>&1
```

Everything else is for looking at what it found and for feeding it.

## Reading results

### `cdisc report`

Prints candidate clusters first, then leads outside clusters.

```bash
cdisc report                          # everything open
cdisc report --new                    # changed since the last --ack
cdisc report --min-confidence probable
cdisc report --md --out reports/today.md
cdisc report --json | jq '.[0].summary.evidence'
```

Each cluster block shows the shared lure, shared infrastructure and contacts,
the confidence level with its basis, the evidence list (feature, specificity,
cases), the analyst assessment, the plain-language reasons, recommended
pivots, and the member cases with their URLs.

The leads section lists single cases with a strong signal of their own:
domains registered in the last 90 days, messaging handles, repositories or
accounts younger than two months, wallets. On the first run these were the
most actionable output.

### `cdisc case CASE-0110`

Drills into one case:

```text
CASE-0110  [reddit:remotework]  relevance=0.836  lure=advance_fee  stages=screening,onboarding,monetisation
title: Is this job a scam? Movate remote call center rep
clusters: -

lure text:
  · if your equipment meets the requirements you'll be invited to a final interview with a hiring manager via microsoft teams
  · we need you to run thinscale validation tool using the computer or laptop that you will be using

observables:
  domain       links-to     ipaddressworld.com
  email        contact      jovi.defeo@movate.com
  ip           links-to     104.21.85.24              (derived by DNS enrichment)
  nameserver   links-to     vera.ns.cloudflare.com    (derived by DNS enrichment)
  phrase       phrase       if your equipment meets the requirements you'll be invited to a final interview ...

generated queries:
  reddit  pending  "you'll move on to the onboarding process"
  reddit  pending  ipaddressworld.com
  web     manual   "ipaddressworld.com"
```

The last section shows the pivots this case spawned. Reddit and GitHub queries
run automatically on the next cycle; web queries are for you. `--body` prints
the full text, `--json` the whole record.

### `cdisc search`

Full-text search across all cases, FTS5 syntax:

```bash
cdisc search '"training fee" OR "training period"'
cdisc search 'telegram AND "coding challenge"'
```

Use it to check whether a phrase from a new report already exists in the
corpus before treating it as new.

### `cdisc assess`

Records your call on operator lineage. The engine never sets this field, and it
never changes the engine's confidence.

```bash
cdisc assess CLU-0001 --lineage unknown --note "one URLhaus reporter, three per-OS payload paths on one Vercel host"
cdisc assess CLU-0007 --lineage same --note "shared handle + identical script + same payload host"
```

Re-running replaces the previous record. The cluster status moves to
`investigating`.

### `cdisc stats`

Counts per table, observables by type, cases by source, and each collector's
last run.

## Feeding it

```bash
cdisc add-url https://www.linkedin.com/posts/...          # fetches and ingests the page
cdisc add-text -t "WhatsApp recruiter transcript" < chat.txt
cdisc add-text -t "Screenshot transcription" --url https://x.com/... --date 2026-09-05 < notes.txt
```

Manual items are trusted: they skip the relevance gate and are processed into
a case immediately unless `--no-process` is given. This is the path for
LinkedIn, X, Threads, WhatsApp and anything else the collectors cannot reach.

## The pivot queue

```bash
cdisc pivot --list            # queue in run order: pending first, by priority
cdisc pivot --limit 5         # run a few by hand
cdisc pivot --regenerate      # rebuild the queue from all cases after rule changes
```

Queries are prioritised: contact handles first, then lure phrases, then domains
and emails, then brand names. Cases that came from articles (RSS, GitHub) only
contribute wording, never the domains they cite. Reddit queries run against the
subreddits in `pivot.reddit_subreddits`; each subreddit is one throttled Arctic
Shift request, so the default is two.

## Tuning the profile

The profile is the theme. Edit the copy `cdisc init` made:

```text
data/profiles/recruitment-threat/lexicon.yaml   terms, lure rules, brands, templates, labels
data/profiles/recruitment-threat/sources.yaml   venues, feeds, thresholds, specificity map
```

Then:

```bash
cdisc reprocess              # re-score and re-extract every stored item; enrichment kept
cdisc pivot --regenerate     # rebuild queries from the new cases
cdisc cluster                # rebuild clusters and evidence
cdisc report
```

`reprocess` takes seconds and makes no network calls, so tuning a weight and
checking the effect is a tight loop. Useful checks after a change:

```bash
cdisc stats                                      # did the case count move the way you expected
cdisc search 'scam' --limit 50                   # sample what passed
```

## Stages on their own

```bash
cdisc collect --source reddit --days 7
cdisc process
cdisc enrich --provider dns --provider rdap
cdisc pivot
cdisc cluster
cdisc report
```

## Profiles

```bash
cdisc profiles                       # list, active marked *
cdisc --profile <name> run           # or export CDISC_PROFILE=<name>
```

A new theme is a new directory under `campaign_discovery/profiles/` with its
own `lexicon.yaml` and `sources.yaml`. The engine code does not change.

## Optional model review

```bash
pip install -e ".[llm]"
cdisc interpret CLU-0001
```

Sends one cluster's evidence package to the model and stores its notes on the
cluster: whether the cases cohere, key similarities, outliers, investigation
questions, suggested queries (which join the pivot queue). It is instructed
never to assert operator identity. The analyst assessment stays yours.

## A realistic session

1. Cron ran overnight. `cdisc report --new` shows one probable cluster and two
   fresh leads.
2. `cdisc case` on each member. Two reports quote the same onboarding script and
   one names a Telegram handle.
3. `cdisc search '"@that_handle"'` finds an older report the clusterer missed
   because it predates an extraction rule you now realise is missing.
4. Add the rule in the lexicon, `cdisc reprocess`, `cdisc cluster`. The third
   case joins; confidence moves to `strongly_clustered` because the handle is a
   second independent high-specificity feature.
5. Run the listed web pivots by hand; add what you find with `add-url`.
6. `cdisc assess CLU-0007 --lineage unknown --note "shared handle and script; hosting differs, no payload overlap yet"`.
7. `cdisc report --ack` to clear the new flags.

## Where things live

```text
data/cdisc.db                     SQLite: raw items, cases, observables, relationships,
                                  search terms, clusters, enrichment (FTS5 over cases)
data/raw/<source>/<id>.json       original payload of every collected item
data/profiles/<profile>/          your editable copy of the profile
reports/                          markdown reports written by `run` and `report --out`
```
