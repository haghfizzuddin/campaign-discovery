# Methodology

campaign-discovery automates the *triage* stage of campaign research on public
sources. It does not investigate and it does not attribute. Its job is to say:
"these N reports probably belong together, and here is why", then hand the
evidence to an analyst.

## The loop

```text
collect  ->  gate  ->  extract  ->  enrich  ->  group  ->  evidence  ->  review
   ^                                                                     |
   +------------------------- pivot queries -----------------------------+
```

1. **Collect.** Pull public reports and feeds on a schedule (Reddit via the
   Arctic Shift archive, GitHub search, RSS, Mastodon public tags, URLhaus) and
   accept manual evidence from platforms that block automation. Everything is
   kept raw before any judgement is made.
2. **Gate.** Score each item against the active profile: a topic context (for
   the recruitment profile, employment vocabulary) *and* a harm context (fraud
   or malware vocabulary), or a named campaign. Items below the threshold stay in
   the evidence store with their score. The gate is a tunable YAML lexicon, not
   a model, so a miss can always be explained and fixed.
3. **Extract.** Indicators (domains, URLs, IPs, emails, messaging handles, code
   repositories, wallets, hashes), impersonated brands, and normalised lure
   sentences. Each becomes an observable linked to the case with a role.
4. **Enrich.** DNS, RDAP registration data, repository and account metadata,
   dependency manifests, optional abuse.ch and IntelOwl. Derived facts (resolved
   IPs, nameservers, registrars, uncommon dependencies) become observables too,
   so infrastructure a victim never mentioned can still connect reports.
5. **Group.** Cases that share observables or lure wording form candidate
   clusters. Shared features are graded by specificity and independence
   (see `evidence-model.md`); hubs shared by a large fraction of all cases are
   ignored; features that rest on a single kind of weak evidence do not link.
6. **Evidence.** Every cluster carries a structured evidence list and a
   confidence level derived from how many *independent, high-specificity*
   features stack. The report renders the same evidence as prose reasons.
7. **Review.** The analyst reads the evidence package, looks for alternative
   explanations (shared hosting, cross-posting, copied advice text), and records
   an assessment. Confidence never becomes attribution on its own.
8. **Pivot.** Each case generates prioritised search queries (contact handles
   first, brand names last). Automated queries run against Reddit and GitHub on
   the next cycle; web queries are listed for the analyst. Hits re-enter the
   loop as raw items, depth-limited.

## Design rules

- Deterministic code does collection, extraction, enrichment and grouping.
  A language model, if used at all, only interprets a finished evidence package
  and must not assert operator identity.
- The engine knows *how* to discover; a profile knows *what matters* for a
  theme. Theme words, brands, templates and labels live in the profile.
- A feature enters the engine only after at least two real research cases need
  it. Until then it stays in a profile or outside the repository.
- Same infrastructure, same code or same wording alone do not prove operator
  identity. The engine reports overlap; lineage is an analyst field.
