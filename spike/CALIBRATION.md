# Calibration record — 2026-09-27, jev-1.13.0

Ground truth by rule, never by the model: removed parameters (callee == the finding's object and the
keyword is passed), removed objects (reference resolves through the consumer's imports and the library's
re-exports to the finding's module). Positional moves and "parameter added as required" are decided by
signature diffs in the engine and are not calibrated here. Evidence: `plain` = the call statement with 3
lines of context, comments stripped; `truncated` = only the line holding the keyword/name.

| set | labelled | reach acc @0.5 | reach ≥0.8 truly reached | reach <0.4 truly reached | break conf≥0.9 acc |
|---|---|---|---|---|---|
| httpx 0.27.2→0.28.0, plain | 40 (14+) | 40/40 | 13/13 | 0/25 | 14/14 |
| httpx, truncated | 40 | 38/40 | 13/14 | 0/23 | 14/14 |
| click 8.1.8→8.2.0, plain | 8 (7+) | 8/8 | 7/7 | 0/1 | 7/7 |
| click, truncated | 8 | 7/8 | 6/6 | 1/1 miss at <0.2 | 6/6 (one conf<0.5 answer, wrong) |

Findings that changed the design:
1. Evidence quality dominates calibration. With the callee visible the answer is bimodal and the
   0.2–0.8 band is nearly empty; with one line it is not, and each tail picks up ~1 error in 40.
   The renderer therefore always includes the whole call statement.
2. Two apparent model failures were question/label defects: "reach" is the wrong question for a
   positional move (the consumer never passes the moved keyword), and a removed `click.parser.Option`
   was being matched against `click.Option` (= `click.core.Option`) by both the label and the evidence.
   Both moved into deterministic code (`cie/positional.py`, `reach.resolve`).
3. Thresholds: REACH_YES 0.8, REACH_NO 0.4, BREAK_AUTO 0.8. Supported by the data above; re-run
   `spike/calibrate.py` after any change to the questions, the renderer, or the model version.

Cost: ~200 requests, ~$0.006.

## Re-calibration 2026-09-29 — pooled with a repository-history benchmark

`spike/history_bench.py` walks a repo's first-parent history, takes every commit whose library package
had handled API findings, uses the repo at the parent as the consumer and the commit's own diff as truth
(a candidate line the commit changed or deleted = break). Run over five package surfaces in two private
repos: 4 commits with findings, 252 candidate spans, 208 decided by rule (198 breaks all confirmed by the
diff; 10 compatible of which 2 were changed lines — traced to the newly-required-parameter rule being a
"passes anything" heuristic, now an exact count against the new signature), 44 by the model.

Pooled model-judged spans with truth, plain evidence (public 48 + private 44 = 92):

| reach band | n | truly reached |
|---|---|---|
| [0.0,0.2) | 18 | 0.00 |
| [0.2,0.4) | 8 | 0.00 |
| [0.4,0.6) | 2 | 0.50 |
| [0.7,0.8) | 4 | 1.00 |
| [0.8,1.0] | 60 | 1.00 |

Precision at REACH_YES 0.5/0.6/0.7/0.8 = 1.000 over 65/64/64/60; nothing truly reached below 0.5.
Decision: REACH_YES 0.8 → **0.7** (every observation at or above it was real; [0.6,0.7) has no data
yet), REACH_NO stays **0.4**, BREAK_AUTO stays **0.8** (all truly-reached answers were at conf ≥ 0.9).
Pooled precision on the history benchmark: 238/238 verified consequences were lines the commits
changed; recall 240/251 references (96%). Model cost of the whole benchmark: $0.0015.

## 2026-09-29 live-PR observation (private repo, not pooled)

Three `settings.<member>` spans whose receiver was imported from a different package (`from
shop_engine.config import settings` vs a member removed from `shop_api.config.ApiSettings`) were
answered reach 0.79 / 0.87 / 0.83 by the model — all three false, the first false "reached" answers
above 0.7 seen (previously 64/64). The evidence showed the statement and 3 context lines, not the
import, so the model could not have known. Rule fix, not a threshold change: `reach.member_ok` now
drops a receiver that resolves to a module outside the library. Thresholds
unchanged.

Re-run of the private history benchmark after that fix (same 3 surfaces, same commits): identical 252
spans, 0 dropped, 0 added — the rule removes nothing from this corpus. Four rows changed status, none
from the fix: three removed-parameter calls moved needs_review → verified because REACH_YES is now 0.7
(reach 0.74 / 0.80 / 0.76, all truly changed by the commit), one aggregation call moved
compatible → verified because the added-required rule now counts supplied arguments. Pooled: precision
242/242, touched-but-not-verified 6 → 2, model band check reach ≥0.7 & untouched 0/44, <0.4 & touched 0/44.

## 2026-09-29 PR-pair mining (private estate, four repositories, not pooled)

Method (`spike/pr_pairs.py`, `spike/pr_pair_score.py`): a "fixer" PR cites an earlier PR in a fix-type
title/body, or is a fix-type PR whose removed Python lines were written by one recent PR (blame). cie then
runs on the breaker at its parent, and every verified consequence is either a line the breaker itself
updated (or whose import binding it rewrote) or a predicted ESCAPE, confirmed when the fixer rewrote that
exact statement.

340 pairs. 146 breakers touched no Python package (CI, charts, docs); 135 changed behaviour with no public
API change — outside a signature diff by construction; 59 changed a public API (21 distinct breakers).
On those: 310 verified consequences, all updated by the breaker itself; 0 predicted escapes survived;
5 review items were confirmed by the fixer (a revert). Every escaped fix in this estate was behavioural.

The pairs still paid for themselves — seven false-positive classes and one recall gap surfaced and were
made deterministic: a member reached through a receiver imported from another
package; a module-level function through a local receiver, or a same-named method def taken as an
override; unresolved relative imports; a keyword on a foreign callee, a local class, or a local def that
never calls the library; a common member name the consumer also defines (`description`) — now capped to
review when the receiver type is unknown; a same-file constant with the library's name (eleven per-file
copies of `REQUEST_ID_HEADER`); and a removed parameter passed positionally, which keyword matching never
saw. The last one also added two true rows to the history benchmark.

History benchmark after all of it: 254 spans (252 + 2), 244/244 verified true, 0 false, 2 changed lines not
verified, 210 by rule / 44 by model. Thresholds unchanged.

## 2026-09-30 estate-wide ranking and threshold sweep (private estate, seven repositories)

Corpus: every first-parent commit since the repositories began whose Python packages changed a public API
(44 packages, 45 commits, 826 candidate spans), truth = the commit's own diff, plus 409 breaker/fixer PR pairs
(104 after a public-API change, 48 distinct breaking PRs). Commits changing more than 3,000 lines (whole-repo
imports, a 6.9k-line revert) are reported apart: "the commit touched it" means nothing there.

Ordinary commits: 149 verified consequences, 149 true, 0 false; 2 changed lines naming a removed object were
never surfaced (generated gRPC stubs); 9 changed lines sat in review. Bulk rewrites: 460 true, 2 false (dead code
still broken at HEAD), 176 unsurfaced from one 56k-line import. 73% of all spans decided by rule.

PR pairs: 685 verified consequences, 99% real (updated by the breaking PR, directly or via its import line, or
rewritten by a later PR) against 39% for "every line naming a changed object" (3,509 lines). Predicted escapes
followed forward in history: one confirmed by a 7-file PR 28 days later (a subclass still calling `self.client`
after the base replaced it), one rewritten by a 42-file PR 38 days later, one griffe artefact (a name rebound in
a tuple assignment reported as removed).

Threshold sweep on the 123 model-judged spans of ordinary commits (the rules now decide the rest):

| reach ≥ | verified true / false | review | silent misses |
|---|---|---|---|
| 0.6 | 6–7 / 3–5 | 51–55 | 2 |
| **0.7** | **6 / 0** | **58** | **2** |
| 0.8 | 5 / 0 | 59 | 2 |

The break-confidence gate no longer changes any verdict between 0.5 and 0.9 once reach ≥ 0.7. Lowering the
not-reached bound from 0.4 to 0.3 removes both silent misses at +16 review items; both are generated-code edge
cases, so 0.7 / 0.4 / 0.8 stay. Fifteen further rules came out of this pass (2026-09-29 → 30): definitions are
never uses; a reference whose import resolves to the removed object is decided by rule; importing a deleted
module is a consequence; with-as / for / tuple-unpacking / parameter bindings shadow a removed name; module-scope
bindings nested in if/try count as the file's own; `Class.__init__(p)` findings match the class call.

Cross-language lenses on the same estate: `cie routes` (FastAPI paths with include_router prefixes vs URL
literals in a TypeScript UI) — 202 routes left the estate since July, the UI names none of them at HEAD; the
four false hits on the way (a route that moved under a mount prefix, one that moved to another service, a comment,
a one-segment router path matching a regex literal) are each now excluded. `cie config-contract` (pydantic
settings with env_prefix and aliases vs the deployed chart): across eight apps no required key is missing.

Route contract through history (same estate, one UI consumer): of 110 routes the services dropped since April, the
UI named 14 lines at the time and rewrote all 14 within a day — coordinated changes, no escape; at HEAD the UI names
one dropped route, a map entry nothing references, reported as a stale constant. Resolving mounts across files and
repositories, factory-built library routers (one router, two mount paths), and never-referenced map entries each
removed a false hit and each is a rule now.

## 2026-09-30 executed truth (private estate, one repository, 18 commits)

The history benchmarks call a line "true" when the commit changed it. To measure without that proxy: overlay only
the files that define the changed objects from commit C onto the repository at C^1, so every caller in every
package is still written against the old API, then run the repository's own test suites (one pytest per package).
Result over 18 commits with cross-file consequences: 79 verified lines outside the definition files, 68 raised when
executed — 9 at the exact line, 59 because their package failed at import (the deleted or reshaped module takes
the whole package down, and every consumer of it) — 11 never reached by any test, 0 ran without raising. Of 47
review lines, 2 raised. One raising frame cie had not flagged: the import line of a removed object, whose uses it
had put in review; that line is where the failure surfaces first and is now a rule (import-line consequences add 30
true rows to the reference benchmark, 0 false). Manifest consumer lists are now derived from imports across the
estate (spike/derive_consumers.py); the hand-written lists matched for four of five libraries.

## 2026-09-30 first live contradiction (private estate, one open PR)

The estate-wide sweep flagged two verified breaks on a live PR: `Model.column` references where the removed member
lived on a different class of the same package. The receiver was an imported class — deterministic, never the
model's call — so a rule now drops a member reached through an imported class other than the finding's own
(reference benchmark unchanged: 276 rows, 0 changes). The comment was removed within minutes under the
only-where-it-breaks policy; the four remaining review items on that PR are ORM instances of unknown type, which is
what review is for. Sweep result at that point: 38 PRs, 1 flagged, 0 true.

## 2026-09-30 a break the contract lens cannot see, and the row added for it

A merged refactor introduced a new env-backed setting with a default — a server-side statement timeout of 900 s
applied to every connection pool. No signature changed, so no lens fired. A stored procedure that runs 20–25
minutes was killed at exactly 900 s; the fix landed nine days later (a per-pool override plus the chart setting
the key). The PR comment now carries a "settings defaults" row: every new env-backed setting and every changed
default in a touched settings file (pydantic fields behind their env_prefix and aliases, rename-aware), with the
charts that set the key — an unset new key is marked as applying to every deployment. Replayed on the merged
PR it shows two new keys, the timeout among them; at the time no chart set either. Advisory (⚠️), never a
verdict: only a person knows that a procedure runs longer than a default.

Checked the same class through history (every settings module change since April, 764 new or changed defaults across
four repositories, each followed forward): eight more incidents of the same shape — a default the deployed chart did
not set, or pinned at the old value — fixed between 2 and 23 days later by a chart or code change naming the key. The
"settings defaults" row would have named every one at PR time; it now also says when a chart pins a changed default
at its old value, which was the schema case.

The sibling class — defaults of function parameters and class attributes, 224 changes since April — produced no
confirmed incident: every forward hit was a version bump, prompt text, a logger, a tool rename or a coincidence.
Settings defaults are where the behaviour hides; function defaults were not, on this estate.

## 2026-10-02 public estate: pydantic and Prefect history (public repositories only)

`spike/history_bench.py` on public repositories (full clones, truth = the commit's own diff, rules plus the model,
$0.048 of model spend in total). The first run of each exposed rule gaps that the private estate never hit; each was
fixed by rule with a test, then the benchmark re-run.

| run | commits | verified | false | notes |
|---|---|---|---|---|
| pydantic, first | 24 | 31 | 17 (45% precision) | same-named members in the changed module (decode/encode/__hash__ on other classes), an unknown receiver the consumer also defines, a name still bound under `if TYPE_CHECKING` / `else` |
| pydantic, after | 24 | 10 | 0 (100%), recall 14/16 | the name still exported through `__all__` or a lazy table is a review item, never verified (the model, shown one statement, said reach 0.93) |
| Prefect, first | 16 (partial) | 165 | 8 | `serve()` imported from another module, `prefect.context.X` read as an instance, `build_image()` defined in the calling file |
| Prefect, after | 20 | 182 | 1 | the one is a real escape: `scripts/generate_cli_docs.py` imported `prefect.cli.root`, removed by the CLI promotion (#20838, 2026-02-25), repaired by #22503 on 2026-07-20 (145 days) |

Prefect without the bulk rewrite (`e977a2ca`, typer to cyclopts): 19 commits, 74 verified, 74 true, nothing unsurfaced.
The bulk commit alone: 108 verified, 107 true, 267 changed lines in the new CLI package never surfaced (modules replaced
wholesale; reported apart as in the private ranking). Of 1,161 model-judged spans below reach 0.4, 67 sat on lines the commit
touched — the removed definitions themselves, which the model correctly calls not a use.

Rules added from this data: a module-level object reached through a receiver or an import must come from the finding's
module or a package above it; a receiver that resolves to a module of the library (`contracts.library_modules`) is never a
class instance; a member of the changed module's own name does not resolve by name; a member through an ambiguous
receiver is never decided by a positional rule; a 'removed' name still bound or exported at the new ref goes to a person;
a bare name the file defines itself is not the library's. Not re-run: the private reference benchmark (not available on
this machine); every change moves findings from verified to review, never the other way.

Config lens on the public Helm chart (prefect-helm, `charts/prefect-server`): `read by code` 52 → 540 keys once an
`env_prefix` built by a helper function, a `...BaseSettings` subclass and `AliasChoices` are understood; 'set by chart, read
nowhere' fell from 11 to 1 (`HOME`).
