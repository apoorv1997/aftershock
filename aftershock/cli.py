"""aftershock — affected vs verified consequence, with the evidence path. Never a score."""
from __future__ import annotations

import argparse
import collections
import json
import sys

from . import rules, budget, config_contract, contracts, evidence, explain as explain_mod, named_queries, positional, prcheck, reach, routes as routes_mod, verify


def api_impact(a):
    findings = contracts.griffe_findings(a.package, a.old, a.new, cwd=a.lib, search=a.search)
    exports = contracts.package_exports(a.lib, a.package, a.old, a.search)
    print(f"Contract diff {a.package} {a.old} -> {a.new}: {len(findings)} findings")
    pkg_dir = f"{a.search + '/' if a.search else ''}{a.package}"
    behind, ahead = contracts.commits_behind(a.lib, a.old, a.new, pkg_dir)
    if behind:
        print(f"  NOTE: {a.new} lacks {behind} commit(s) that touch {pkg_dir} on {a.old} — behavioural fixes "
              f"in those are invisible to a signature diff; this report covers API changes only")
    consumer = a.consumer
    if getattr(a, "github", False):
        import pathlib, tempfile
        from . import github_reach
        consumer = a.consumer or tempfile.mkdtemp(prefix="aftershock-github-")
        print(f"GitHub code search (throttled to 10/min) -> {consumer}")
        stats = github_reach.build_consumer_tree(findings, a.package, pathlib.Path(consumer),
                                                per_query=a.per_query, max_queries=a.max_queries)
        print(f"  fetched {stats.pop('_files')} files across {len(stats)} queries")
    modules = contracts.library_modules(a.lib, a.package, a.old, a.search)
    spans = reach.candidates(consumer, findings, exports=exports, skip_vendored=getattr(a, "github", False), modules=modules)
    if a.no_tests:
        spans = [s for s in spans if not reach.is_test_path(s.file)]
    print(f"Affected (candidate spans in {consumer}): {len(spans)}")
    for f, n in reach.default_changes(consumer, findings).items():
        if n:
            print(f"  behaviour change, not verified per call: {f.describe()} — {n} calls omit `{f.parameter}`")
    print()
    if a.no_verify:
        for s in spans:
            print(f"  {s.file}:{s.line}  {s.finding.describe()}")
        return
    report(spans, findings, a)


def report(spans, findings, a):
    # positional moves and newly-required parameters are decided by signature rules, never by the model
    det, ask = [], []
    for s in spans:
        v = rules.decide(s, a.lib, a.old, a.new)
        if v is not None:
            det.append((s, v))
            continue
        ask.append(s)
    states = [evidence.render(s, a.package, a.old, a.new, keep_comments=a.keep_comments) for s in ask]
    b = budget.Budget(run_usd=a.budget_usd)
    est = len(states) * 800 * budget.PRICE_PER_TOKEN
    print(f"{len(states)} spans need the model (~${est:.4f} at ~800 tokens each); "
          f"{len(det)} decided by rule. Run cap ${b.run_usd:.2f}, cumulative cap ${b.total_usd:.2f} "
          f"(spent so far ${b.prior_usd:.4f}).")
    verdicts = verify.cap_ambiguous(ask, verify.verify(states, budget=b))
    b.close(label=f"{a.package} {a.old}->{a.new}")
    print(b.summary(), "\n")
    groups = collections.defaultdict(list)
    for s, v in list(zip(ask, verdicts)) + det:
        groups[v.status].append((s, v))
    spans, verdicts = ask + [s for s, _ in det], verdicts + [v for _, v in det]
    order = ["breaks_consumer", "needs_review", "compatible", "unclear", "not_reached"]
    github = getattr(a, "github", False)
    md = getattr(a, "format", "text") == "md"
    titles = {"breaks_consumer": "Verified consequence", "needs_review": "Needs human confirmation",
              "compatible": "Verified compatible", "unclear": "Evidence insufficient",
              "not_reached": "Not actually reached (look-alike)"}
    if md:
        print(f"### aftershock: {a.package} {a.old} → {a.new}\n")
        counts = ", ".join(f"{len(groups[s])} {titles[s].lower()}" for s in order if groups[s])
        print(f"{len(findings)} API findings · {len(spans)} candidate spans · {counts}\n")
    for status in order:
        if not groups[status]:
            continue
        title = titles[status]
        # production code first; a broken test is caught by CI and rarely the consequence anyone acts on
        prod = [x for x in groups[status] if not reach.is_test_path(x[0].file)]
        tests = [x for x in groups[status] if reach.is_test_path(x[0].file)]
        groups[status] = prod + tests
        repos = {s.file.split("/")[0].replace("__", "/") for s, _ in groups[status]} if github else set()
        head = (f"{title} ({len(prod)} in production code, {len(tests)} in tests"
                f"{f', {len(repos)} repos' if github else ''})")
        print(f"<details{' open' if status in ('breaks_consumer', 'needs_review') else ''}><summary><b>{head}</b></summary>\n"
              if md else f"{head}:")
        shown = groups[status] if status in ("breaks_consumer", "needs_review") or not github else groups[status][:5]
        for s, v in shown:
            loc = s.file.replace("__", "/", 1) if github else s.file
            how = "rule" if v.input_tokens == 0 and v.break_confidence == 1.0 else f"reach {v.reach:.2f}, break {v.break_choice} ({v.break_confidence:.2f})"
            if md:
                print(f"- `{loc}:{s.line}` — {s.finding.describe()} — *{how}*")
            else:
                print(f"  {loc}:{s.line}  {s.finding.describe()}")
                tag = "  [ambiguous: the consumer defines a same-named member; receiver type unknown]" if v.ambiguous else ""
                print(f"      reach={v.reach:.2f}  break={v.break_choice} ({v.break_confidence:.2f}){tag}")
        if len(shown) < len(groups[status]):
            print(f"{'- ' if md else '  '}... {len(groups[status]) - len(shown)} more")
        print("\n</details>\n" if md else "")
    tok = sum(v.input_tokens for v in verdicts)
    print(f"{'_' if md else ''}{len(verdicts)} verifications, {tok} input tokens (~${tok * 0.042 / 1e6:.4f}){'_' if md else ''}")
    if a.json:
        json.dump([{"file": s.file, "line": s.line, "finding": s.finding.describe(), "status": v.status,
                    "reach": v.reach, "break": v.break_choice, "confidence": v.break_confidence}
                   for s, v in zip(spans, verdicts)], open(a.json, "w"), indent=1)
    if getattr(a, "explain", False):
        kept = [{"file": s.file, "line": s.line, "finding": s.finding.describe(),
                 "how": "rule" if v.input_tokens == 0 and v.break_confidence == 1.0 else "calibrated check",
                 "code": evidence.strip_comments(s.context), "stmt": s.code.strip().splitlines()[0] if s.code.strip() else ""}
                for s, v in zip(spans, verdicts) if v.status == "breaks_consumer" and not (a.no_tests and reach.is_test_path(s.file))]
        review = [{"file": s.file, "line": s.line, "finding": s.finding.describe(),
                   "reason": "reach or confidence in the review band" if v.input_tokens else "budget cap"}
                  for s, v in zip(spans, verdicts) if v.status == "needs_review" and not (a.no_tests and reach.is_test_path(s.file))]
        change = {"library": a.package, "old_version": a.old, "new_version": a.new,
                  "findings": sorted({s.finding.describe() for s in spans})[:40],
                  "public_names": sorted(contracts.public_names(a.lib, a.package, a.new, a.search))}
        print("\n" + ("## Explanation\n" if md else "=== Explanation ===\n"))
        ex = explain_mod.explain(change, kept, review, model=a.explain_model, max_usd=a.explain_budget_usd,
                                 backend=a.explain_backend)
        print(ex.text)
        print(f"\n{'_' if md else ''}explained by {ex.model}: {ex.input_tokens} in / {ex.output_tokens} out tokens ≈ ${ex.usd:.4f}"
              f"{' — ' + str(len(ex.unknown_refs)) + ' invented location(s) flagged' if ex.unknown_refs else ''}"
              f"{' — ' + str(len(ex.unknown_apis or [])) + ' unverified API(s) flagged' if ex.unknown_apis else ''}{'_' if md else ''}")
        if ex.usd:   # same ledger as the verification calls, so the cumulative cap sees both
            budget.LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with budget.LEDGER.open("a") as f:
                import time as _time
                f.write(json.dumps({"ts": _time.strftime("%Y-%m-%dT%H:%M:%S"), "label": f"explain {a.package} {ex.model}",
                                    "tokens": ex.input_tokens + ex.output_tokens, "usd": round(ex.usd, 6)}) + "\n")


def named_queries_cmd(a):
    old = named_queries.registry(a.lib, a.old, a.src)
    new = named_queries.registry(a.lib, a.new, a.src)
    if not new and old:
        print(f"Registry directory {a.src} is ABSENT at {a.new} ({len(old)} queries at {a.old}): the branch "
              f"predates the registry or moved it — not a removal of every query. Rebase it and re-run.")
        return
    if not old and not new:
        print(f"No named queries found under {a.src} at either ref; check --src."); return
    changes = named_queries.diff(old, new)
    print(f"Named-query contract {a.old} -> {a.new}: {len(old)} -> {len(new)} queries, {len(changes)} changes")
    for ch in changes:
        print(f"  {ch.kind:16s} {ch.name}: {ch.detail}")
    sites = named_queries.call_sites(a.consumer, names=set(old) | set(new))
    if a.no_tests:
        sites = [s for s in sites if not reach.is_test_path(s.file)]
    injected = named_queries.injected_keys(a.consumer)
    if injected:
        print("Keys injected by a shared client wrapper (count as sent by every call site): " + ", ".join(
            f"`{k}` @ {i.file}:{i.line}{' (conditional)' if i.conditional else ''}" for k, i in injected.items()))
    verdicts = named_queries.verify(sites, old, new, injected=injected)
    # optional parameters: say per call site whether it is sent, sent via the wrapper, or omitted
    for ch in changes:
        if ch.kind != "optional_added":
            continue
        p = ch.detail.split("`")[1]
        for s in sites:
            if s.name != ch.name:
                continue
            how = ("sends it" if s.keys and p in s.keys else
                   f"sent via wrapper {injected[p].file}:{injected[p].line}{' (conditional)' if injected[p].conditional else ''}" if p in injected else
                   "payload not literal" if s.keys is None else "OMITS it — runs with the old behaviour")
            print(f"  optional `{p}` on {ch.name}: {s.file}:{s.line} {how}")
    print(f"\nConsumer {a.consumer}: {len(sites)} call sites of {len({s.name for s in sites})} distinct queries\n")
    for status, title in (("breaks_consumer", "Verified consequence"), ("needs_review", "Needs human confirmation"),
                          ("compatible", "Compatible"), ("not_reached", "Not a registry query")):
        vs = [v for v in verdicts if v.status == status]
        if not vs:
            continue
        print(f"{title} ({len(vs)}):")
        for v in vs if status != "compatible" else vs[:8]:
            print(f"  {v.site.file}:{v.site.line}  {v.site.name}  — {v.reason}")
        if status == "compatible" and len(vs) > 8:
            print(f"  ... {len(vs) - 8} more")
        print()
    print("no model calls: this contract is fully deterministic")


def config_cmd(a):
    c = config_contract.scan(a.code, a.chart, settings_file=a.settings)
    print(f"read by code: {len(c.read_by_code)}   set by chart: {len(c.set_by_chart)}")
    print(f"set by chart, read nowhere ({len(c.set_but_unread)}): {' '.join(sorted(c.set_but_unread))}")
    req = sorted((c.required_by_code or set()) - c.set_by_chart)
    print(f"REQUIRED by code (no default), not set by chart ({len(req)}): {' '.join(req)}")
    if a.show_unset:
        print(f"read by code, not set by chart ({len(c.read_but_unset)}): {' '.join(sorted(c.read_but_unset))}")


def routes_cmd(a):
    old = routes_mod.routes(a.lib, a.old, a.search, a.package)
    new = routes_mod.routes(a.lib, a.new, a.search, a.package)
    d = routes_mod.diff(old, new)
    print(f"Route contract {a.old} -> {a.new}: {len(old)} -> {len(new)} routes, {len(d['removed'])} removed, {len(d['added'])} added")
    for r in d["removed"]:
        note = "" if routes_mod.matchable(r.path) else "   [one segment: include_router prefix unknown, not matched]"
        print(f"  removed  {r.method.upper():8s} {r.path}   ({r.file}:{r.line} {r.function}){note}")
    if not a.consumer:
        return
    hits = routes_mod.consumers(a.consumer, [r.path for r in d["removed"]], tuple(a.ext.split(",")))
    broken = {p: h for p, h in hits.items() if h}
    print(f"\nConsumer {a.consumer}: {sum(len(h) for h in broken.values())} lines still name a removed route "
          f"({len(broken)} of {len(d['removed'])} removed routes reached)")
    for r in d["removed"]:
        for f, ln, code in hits.get(r.path, []):
            print(f"  {f}:{ln}  {r.method.upper()} {r.path}  |  {code}")
    print("\nno model calls: this contract is fully deterministic")


def main(argv=None):
    p = argparse.ArgumentParser(prog="aftershock")
    sub = p.add_subparsers(required=True)
    s = sub.add_parser("api-impact", help="library API diff -> consumer candidates -> verified consequences")
    s.add_argument("--lib", required=True, help="library checkout (git repo)")
    s.add_argument("--package", required=True, help="package path/name for griffe, e.g. httpx or click")
    s.add_argument("--search", help="griffe -s search path, e.g. src")
    s.add_argument("--old", required=True); s.add_argument("--new", required=True)
    s.add_argument("--consumer", help="consumer source tree (omit with --github to use a temp tree)")
    s.add_argument("--github", action="store_true", help="also search public GitHub code for consumers (needs gh auth)")
    s.add_argument("--per-query", type=int, default=100, help="code-search results per finding query")
    s.add_argument("--max-queries", type=int, default=12, help="cap on distinct search queries (10/min limit)")
    s.add_argument("--no-verify", action="store_true", help="list candidates only, no model calls")
    s.add_argument("--budget-usd", type=float, default=0.02,
                   help="hard cap on model spend for this run (default $0.02); AFTERSHOCK_MAX_SPEND_USD caps all runs (default $1)")
    s.add_argument("--keep-comments", action="store_true")
    s.add_argument("--format", choices=["text", "md"], default="text", help="md = GitHub-flavoured markdown for a PR comment")
    s.add_argument("--explain", action="store_true", help="prose for the engineer over verified consequences only")
    s.add_argument("--explain-backend", choices=["template", "ollama", "anthropic"], default="template",
                   help="template = deterministic, free (default); ollama = local open model, free; anthropic = Claude, paid")
    s.add_argument("--explain-model", default=None, help="ollama model tag or Claude model id (backend defaults otherwise)")
    s.add_argument("--explain-budget-usd", type=float, default=0.05, help="skip the explanation if its input alone would cost more")
    s.add_argument("--no-tests", action="store_true", help="ignore consumer test code (tests/, test_*.py, conftest.py)")
    s.add_argument("--json")
    s.set_defaults(fn=api_impact)
    q = sub.add_parser("named-queries", help="named-query registry contract vs a consumer's call sites")
    q.add_argument("--lib", required=True, help="checkout of the repository that holds the registry")
    q.add_argument("--src", required=True, help="registry directory in that repository")
    q.add_argument("--old", required=True); q.add_argument("--new", required=True)
    q.add_argument("--consumer", required=True); q.add_argument("--no-tests", action="store_true")
    q.set_defaults(fn=named_queries_cmd)
    c = sub.add_parser("config-contract", help="env keys read by code vs set by chart")
    c.add_argument("--code", required=True); c.add_argument("--chart", nargs="+", required=True)
    c.add_argument("--settings", help="a Settings class file whose UPPER_CASE fields are env-backed")
    c.add_argument("--show-unset", action="store_true")
    c.set_defaults(fn=config_cmd)
    r = sub.add_parser("routes", help="HTTP route paths a Python service exposes, old vs new, against URL literals in any consumer (a TypeScript UI); no model")
    r.add_argument("--lib", required=True); r.add_argument("--package", default=""); r.add_argument("--search", default="")
    r.add_argument("--old", required=True); r.add_argument("--new", required=True); r.add_argument("--consumer")
    r.add_argument("--ext", default=".ts,.tsx,.js,.jsx,.py,.vue")
    r.set_defaults(fn=routes_cmd)
    k = sub.add_parser("pr-check", help="one PR, every applicable lens from an estate manifest, one comment (printed; --post sends it)")
    k.add_argument("--estate", default="~/.config/aftershock/estate.json"); k.add_argument("--repo", required=True, help="repo name in the manifest")
    k.add_argument("--pr", type=int, required=True); k.add_argument("--post", action="store_true")
    k.add_argument("--only-breaks", action="store_true", help="post only when something breaks; remove an earlier comment of ours if nothing does")
    k.add_argument("--budget-usd", type=float, default=0.02); k.add_argument("--max-lines", type=int, default=12)
    k.add_argument("--aftershock", default=sys.argv[0]); k.add_argument("--python", default=sys.executable)
    k.set_defaults(fn=prcheck.run)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
