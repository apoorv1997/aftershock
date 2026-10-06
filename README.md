# cie — Change Impact Engine

Evidence-backed consequences of a change, verified. Never a risk score.

```text
what changed (deterministic diff)  ─►  who might use it (AST candidates)  ─►  rendered evidence
                                                                                  │
                            rules decide what rules can; a typed model judges the rest
                                                                                  │
                                       Affected  vs  Verified consequence, with file:line
```

Deterministic first. What changed and who *might* use it come from `griffe` and the AST; whole
classes of change are decided by rules alone (positional moves, newly required parameters,
parameter-kind changes, removed class members used inside their own class, config keys, named-query
signatures). A model — TypeSafe's Jev, which returns probabilities rather than prose — is asked only
the two questions code cannot answer: does this consumer really use the changed API (not a
look-alike), and does the change break it. Anything uncertain is handed to a person, never hidden,
and every model call is capped by a spend budget.

## Commands

No TypeSafe key? The test suite, `named-queries`, `config-contract` and `routes` never call a model, and
`api-impact --no-verify` lists every candidate span without verdicts. Only model verification needs the key.

```bash
export TYPESAFE_API_KEY=...          # https://console.typesafe.ai/keys  (only api-impact needs it)
pip install -e .

# library API change -> consequences in a consumer tree
cie api-impact --lib ../httpx --package httpx --old 0.27.2 --new 0.28.0 --consumer ../my-service
cie api-impact ... --github --per-query 60 --max-queries 12      # public consumers via GitHub code search
cie api-impact ... --no-verify                                     # candidates only, no model calls
cie api-impact ... --no-tests --format md --json out.json          # production code only, PR-comment markdown

# named-query registry contract (registry signatures vs consumer payload keys); no model
cie named-queries --lib ../data-service --src app/queries --old <ref> --new <ref> --consumer ../reporting --no-tests

# env keys the code reads (os.getenv and pydantic Settings: env_prefix, aliases, required vs defaulted) vs keys the chart sets; no model
cie config-contract --code ./service --chart ./helm/apps/service/values-stage.yaml ./helm/apps/service/templates

# HTTP routes a Python service exposes (FastAPI decorators + include_router prefixes), old vs new, against URL
# literals in ANY consumer — a TypeScript UI included; no model. A route that only moved under a mount prefix or
# to another service still answers: spike/routes_estate.py checks "gone from the whole estate".
cie routes --lib ../gateway --old <ref> --new <ref> --consumer ../web-ui --ext .ts,.tsx

# one pull request, every applicable lens, one comment — driven by an estate manifest (which repositories consume
# which library, which chart belongs to which service, who calls whose routes); printed by default, --post sends it
cie pr-check --estate ~/.config/cie/estate.json --repo my-lib --pr 123 [--post]
```

`--explain` adds prose for the engineer — what changed, which shown lines break and why, the smallest fix —
over **verified consequences only**. Three backends (`--explain-backend`): `template` (default; deterministic,
free — the mechanism of every finding kind is known, so no model is needed), `ollama` (a local open model,
free; picks a pulled model or `--explain-model`), `anthropic` (Claude `claude-opus-5`, paid, needs
`ANTHROPIC_API_KEY`, `--explain-budget-usd` skips it above the cap). Model backends never see the repository or
unverified candidates, and four deterministic checks run on their prose: any `file:line` not in the input is
flagged as invented; any `package.Name(` the new library does not define is flagged as an unverified fix (a local
7B model proposed `httpx.Proxies(...)`, which does not exist); a verified location the prose omits is appended, so
summarising never loses a consequence; and a review item named more than once is flagged as mis-attributed (a 14B
model first listed review items among the consequences).

`--budget-usd` caps model spend per run (default $0.02; skipped spans are reported for review, never
guessed). `CIE_MAX_SPEND_USD` caps all runs (default $1) through a ledger at `~/.cache/cie/spend.jsonl`.

## What the report means

| section | meaning |
|---|---|
| Verified consequence | the consumer uses the changed API and the change breaks it — with `file:line` and the finding |
| Needs human confirmation | evidence exists but reach or break confidence sits in the review band, or the budget cap stopped the call |
| Verified compatible | reached, but the use is still valid after the change |
| Not actually reached | a look-alike: same name or keyword on a different object or library |

Production code is listed before test code in every section; `--no-tests` drops tests entirely.
Default-value changes are reported once, in aggregate, never per call.

## Evidence behind the design (2026-09)

- Existing tools already detect public-API breaks: griffe found 37/38 real changes in httpx 0.28 and
  43/46 in click 8.2. cie wraps it rather than re-implementing it.
- Token reach lies: httpx's `app=` on `ASGITransport` looks identical to the removed `Client(app=)`.
  On 50 labelled cases (36 real + 14 adversarial: kwargs dicts, `partial`, wrappers, subclasses,
  aliases) the reach claim scored 50/50, unchanged under 2.7× irrelevant padding and negated phrasing;
  the break claim scored 23/23 on click 8.2 cases.
- Calibration on 48 rule-labelled spans (`spike/CALIBRATION.md`): with the call statement in the
  evidence the reach answer is bimodal and 48/48 at 0.5; with a single evidence line each tail picks
  up ~1 error in 40. Re-calibrated on 92 pooled spans (public + a private repository-history
  benchmark): thresholds reach ≥0.7 / ≤0.4, break confidence ≥0.8.
- A planted misleading comment never flipped a verdict but halved the high-confidence ones, so
  comments are stripped from evidence by default.
- On a real 14-object refactor with the PR's own diff as ground truth: 200/200 verified consequences
  were lines the PR changed (precision 100%), 201/210 references surfaced (recall 96%), 90% decided by
  rule, model cost $0.0007.
- Cross-repo (GitHub code search, first 100 hits per query): 30 hand-checked hits, 18/19 real on true
  consumers; the misses were vendored copies of the library and a same-library look-alike, both now
  excluded deterministically.
- Estate-wide, on seven private repositories and their whole history (44 packages, 826 candidate spans,
  409 breaker/fixer PR pairs): on ordinary commits 149 verified consequences, 149 true, 0 false, 2 changed
  lines never surfaced; 73% of spans decided by rule. Against the PR pairs, 685 verified consequences at 99%
  precision versus 39% for a name grep; the three predicted escapes were followed forward in history — one
  fixed by a targeted PR 28 days later, one 38 days later, one a griffe artefact. Twenty-two look-alike and
  recall classes surfaced by this data are each decided by rule now (`spike/CALIBRATION.md`).
- Across a language boundary: 202 HTTP routes left that estate's Python services since July; the TypeScript
  UI names none of them at HEAD (`cie routes`). Across eight deployed charts, no required env key is missing
  (`cie config-contract`).

Known limits: receivers of unknown type (`x.method(...)`) are matched by method name and land in
review — and when the consumer defines a same-named member on one of its own classes, a model "breaks"
verdict on such a receiver is capped to review (the deciding fact, the receiver's type, is not in the
evidence). A branch whose copy of a library is behind the trunk is API-compatible yet behaviourally
stale — reported as a commits-behind note, since a signature diff cannot see fixes.

## Layout

```text
cie/contracts.py       griffe wrapper, Finding (module-qualified), package re-export map
cie/reach.py           AST candidates: keyword/object/member matching, import + re-export resolver, receivers, aliases
cie/positional.py      rules: positional moves, newly required params, parameter kind changes
cie/evidence.py        JSON state for a claim; comments stripped
cie/verify.py          Jev claims (reach Noul, break Choice) + calibrated thresholds
cie/budget.py          per-run and cumulative spend caps, ledger
cie/github_reach.py    public consumers via GitHub code search (10 searches/min)
cie/named_queries.py   registry-signature HTTP contract vs consumer payload keys
cie/config_contract.py env keys read (os.getenv, pydantic Settings with env_prefix/aliases, required-vs-default) vs set by a chart
cie/routes.py          HTTP route contract: decorator paths + include_router prefixes, old vs new, vs URL literals in any language
cie/rules.py           the one deterministic router (CLI and benchmarks share it)
cie/prcheck.py         pr-check: manifest-driven, in-repo callers + cross-repo consumers + pins + queries + routes + chart, one comment
cie/explain.py         prose over verified consequences only (template / ollama / anthropic) + 4 prose checks
spike/history_bench.py repository-history benchmark: truth = the commit's own diff (callers the author updated)
spike/pr_pairs.py      (breaker PR, fixer PR) miner: a later PR cites or rewrites what an earlier one wrote
spike/pr_pair_score.py cie at the breaker's parent; verified lines split into updated-by-breaker vs predicted escapes
spike/confirm_escapes.py follows each predicted escape forward: the commit that rewrote it (strong / weak) or "still present"
spike/rank.py          per-repository scorecard from both benchmarks; bulk rewrites (>3000 lines) reported apart
spike/routes_estate.py, spike/route_history.py  route contract across a repository boundary, live and through history
spike/                 the spike, calibration harness and record
tests/                 rule-level tests, no network
```

License: MIT (see `LICENSE`).
