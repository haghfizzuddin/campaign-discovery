# Evidence and confidence model

Every candidate cluster stores a structured evidence list. Nothing in a report
is a bare percentage; each link exists because specific features were observed
in specific cases.

## Evidence item

```json
{
  "feature": "telegram",
  "value": "kraken_talent_hr",
  "specificity": "high",
  "cases": ["CASE-0001", "CASE-0002"],
  "group": "kraken_talent_hr"
}
```

- `feature` — the observable type (`domain`, `telegram`, `github_repo`,
  `wallet_eth`, `sha256`, `phrase`, `wording_similarity`, `ip`, ...).
- `specificity` — how unlikely two unrelated reports are to share this value.
  The map lives in the profile's `sources.yaml` under `clustering.specificity`.
- `group` — the independence group. A URL, the domain it sits on, the IPs that
  domain resolves to, its nameservers and its registrar are one group: they are
  one observation of one piece of infrastructure, not five.

Default specificity for the recruitment-threat profile:

| Specificity | Features |
|---|---|
| high | messaging handle, email, wallet, file hash, code repository, full URL |
| medium | domain, code-hosting account, exact lure sentence, lure wording similarity |
| low | IP address, nameserver, dependency, registrar |

## Confidence ladder

Confidence is computed from the number of *independent groups* at each
specificity, never from a similarity percentage.

| Level | Rule |
|---|---|
| `strongly_clustered` | two or more independent high-specificity groups, or one high plus two or more medium |
| `probable` | one high-specificity group, or two or more independent medium groups |
| `possible` | anything else that still formed a cluster |

Two adjustments:

- Low-specificity features only link cases when at least two *kinds* of them
  agree (for example shared IP *and* shared nameserver). Two domains on the same
  cloud provider's IPs is not a link.
- If every case in a cluster was posted by the same account, the cluster is
  capped at `possible` and labelled as a single reporter. Cross-posts within a
  week are folded into one case before clustering anyway.

The ladder deliberately stops at `strongly_clustered`. The engine has no state
for "same operator".

## Analyst assessment

Operator lineage is recorded separately, by a person, after reading the
evidence:

```text
cdisc assess CLU-0001 --lineage same --note "shared Telegram handle and payload host; same script"
cdisc assess CLU-0002 --lineage unknown --note "wording overlap could be copied advice text"
```

`--lineage` takes `same`, `different` or `unknown`. The report shows the
engine's confidence and the analyst's assessment side by side and never merges
them.

## Why this shape

The methodology this engine grew from raises confidence only when independent,
high-specificity characteristics stack. Encoding that as data rather than as a
score means every cluster can be audited later, the same evidence can feed a
future statistical or learned model, and a language model asked to review a
cluster receives the actual features instead of a number.
