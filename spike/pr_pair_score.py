#!/usr/bin/env python3
"""Score cie on (breaker, fixer) PR pairs from pr_pairs.py.

For each pair: the library is every Python package the breaker touched (first parent -> merge commit);
the consumer is the repository just BEFORE the breaker (every caller still written against the old API,
the same vantage as history_bench). cie's verified consequences split into: lines the breaker itself
changed (callers the author updated) and the rest — predicted ESCAPES. An escape is confirmed when the
fixer removed or rewrote a line with the same text in the same file; a fixer line that names a changed
object and was never surfaced is a missed escape. A pair is "in cie's lens" only if the breaker changed a
public API at all (griffe findings of the kinds cie handles); behavioural fixes are counted separately —
they are exactly what a signature diff cannot see.

Usage: pr_pair_score.py <pairs.jsonl> --repo <slug>=<path> [--repo ...] [--budget-usd X] [--out scored.jsonl]"""
from __future__ import annotations

import argparse, ast, collections, json, pathlib, re, subprocess, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from history_bench import changed_ranges, touched, span_touched, span_or_binding_touched, route            # noqa: E402
from cie import budget, contracts, evidence, reach, verify           # noqa: E402

KINDS = ("Parameter was removed", "Public object was removed", "Positional parameter was moved",
         "Parameter was added as required", "Parameter kind was changed")


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout


def packages(repo, sha):
    """(search, package) pairs for the Python packages a merge commit touched (tests excluded): `<x>/src/<pkg>/`,
    `src/<pkg>/`, `<dir>/<pkg>/` or a top-level `<pkg>/` — anything whose directory holds an __init__.py at `sha`."""
    out = set()
    tree = set(git(repo, "ls-tree", "-r", "--name-only", sha).splitlines())
    for f in git(repo, "diff", "--name-only", f"{sha}^1", sha).splitlines():
        if not f.endswith(".py") or re.search(r"(^|/)tests?/", f):
            continue
        parts = f.split("/")
        m = re.match(r"(.*/src|src)/([A-Za-z_]\w*)/", f)
        if m:
            out.add((m.group(1), m.group(2))); continue
        for i in range(min(2, len(parts) - 1)):                      # <pkg>/... or <dir>/<pkg>/...
            search, pkg = "/".join(parts[:i]), parts[i]
            if pkg.isidentifier() and "/".join(parts[:i + 1] + ["__init__.py"]) in tree:
                out.add((search, pkg)); break
    return sorted(out)


def naive_lines(root, names):
    """Baseline: every non-test .py line at the consumer that mentions a changed object's bare name."""
    out = []
    for p in pathlib.Path(root).rglob("*.py"):
        rel = str(p.relative_to(root))
        if reach.is_test_path(rel) or any(x in p.parts for x in (".venv", ".git")):
            continue
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            t = line.strip()
            if t.startswith("#") or not any(re.search(rf"\b{re.escape(n)}\b", t) for n in names):
                continue
            out.append((rel, i))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs"); ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--budget-usd", type=float, default=0.01); ap.add_argument("--out")
    a = ap.parse_args()
    repos = dict(r.split("=", 1) for r in a.repo)
    pairs = [json.loads(l) for l in open(a.pairs)]
    pairs = [p for p in pairs if p["slug"] in repos]
    out = open(a.out, "a") if a.out else None
    tally = collections.Counter()
    for p in pairs:
        repo = repos[p["slug"]]
        pk = packages(repo, p["breaker_sha"])
        if not pk:
            tally["no_python_package"] += 1
            print(f"#{p['fixer']:<4} <- #{p['breaker']:<4} {p['slug'].split('/')[-1]:<15} no Python package touched by the breaker"); continue
        findings = []
        for search, pkg in pk:
            fs = contracts.griffe_findings(pkg, f"{p['breaker_sha']}^1", p["breaker_sha"], cwd=repo, search=search)
            findings += [(search, pkg, f) for f in fs if any(k in f.message for k in KINDS)]
        if not findings:
            tally["behavioural_only"] += 1
            print(f"#{p['fixer']:<4} <- #{p['breaker']:<4} {p['slug'].split('/')[-1]:<15} pkgs={[x[1] for x in pk]} 0 API findings -> behavioural change, outside the lens"); continue
        tally["api_change"] += 1
        bsha = p["breaker_sha"]
        b_ranges, b_gone = changed_ranges(repo, f"{bsha}^1", bsha, "")
        # the fixer's removed / rewritten lines, by text, per file
        fixer_removed = collections.defaultdict(set); cur = None
        for l in git(repo, "diff", "-U0", f"{p['fixer_sha']}^1", p["fixer_sha"]).splitlines():
            if l.startswith("--- a/"):
                cur = l[6:]
            elif l.startswith("--- /dev/null"):
                cur = None
            elif l.startswith("-") and not l.startswith("---") and cur and l[1:].strip():
                fixer_removed[cur].add(l[1:].strip())
        with tempfile.TemporaryDirectory(prefix="cie-pair-") as tmp:
            subprocess.run(f"git -C {repo} archive {bsha}^1 | tar -x -C {tmp}", shell=True, check=False)
            root = pathlib.Path(tmp)
            rows = []
            for search, pkg in pk:
                fs = [f for s, g, f in findings if (s, g) == (search, pkg)]
                if not fs:
                    continue
                exports = contracts.package_exports(repo, pkg, f"{bsha}^1", search)
                spans = [s for s in reach.candidates(root, fs, exports=exports) if not reach.is_test_path(s.file)]
                det, ask = [], []
                for s in spans:
                    v = route(s, repo, f"{bsha}^1", bsha)
                    (det if v else ask).append((s, v))
                b = budget.Budget(run_usd=a.budget_usd)
                verdicts = verify.cap_ambiguous([s for s, _ in ask], verify.verify([evidence.render(s, pkg, "before", "after") for s, _ in ask], budget=b)) if ask else []
                b.close(label=f"pair {p['slug']} #{p['breaker']}->#{p['fixer']}")
                rows += [(s, v) for s, v in det] + [(s, v) for (s, _), v in zip(ask, verdicts)]
            def stmt(s):
                return s.code.strip().splitlines()[0].strip() if s.code.strip() else ""
            ver = [(s, v) for s, v in rows if v.status == "breaks_consumer"]
            in_breaker = [(s.file, s.line) for s, v in ver if span_or_binding_touched(b_ranges, b_gone, "", s, root)]
            escapes = [(s.file, s.line, stmt(s)) for s, v in ver if not span_or_binding_touched(b_ranges, b_gone, "", s, root)]
            esc_hit = [e for e in escapes if e[2] in fixer_removed.get(e[0], ())]
            esc_open = [e for e in escapes if e not in esc_hit]
            rev = [(s.file, s.line, stmt(s)) for s, v in rows if v.status == "needs_review"]
            rev_hit = [r for r in rev if r[2] in fixer_removed.get(r[0], ())]
            names = {f.object_name.split(".")[-1] for _, _, f in findings} - {"<module>", "__init__"}
            naive = naive_lines(root, names) if names else []
            naive_true = sum(1 for fp_, ln in naive if touched(b_ranges, b_gone, "", fp_, ln))
            surfaced = {(s.file, stmt(s)) for s, _ in rows}
            missed = [(fpath, t) for fpath, texts in fixer_removed.items() if fpath.endswith(".py") and not reach.is_test_path(fpath)
                      for t in texts if any(re.search(rf"\b{re.escape(n)}\b", t) for n in names) and (fpath, t) not in surfaced
                      and not t.startswith(("def ", "async def ", "class ", "#", "from ", "import ", '"', "'")) and "`" not in t]
            rec = {"fixer": p["fixer"], "breaker": p["breaker"], "slug": p["slug"], "kind": p["kind"], "packages": [x[1] for x in pk],
                   "findings": [f.describe()[:100] for _, _, f in findings], "spans": len(rows), "verified_in_breaker": in_breaker,
                   "escape_confirmed_by_fixer": esc_hit, "escape_unconfirmed": esc_open, "review": rev, "review_confirmed_by_fixer": rev_hit,
                   "fixer_lines_naming_changed_object_unsurfaced": missed,
                   "naive_lines": len(naive), "naive_true": naive_true, "rows_by_how": collections.Counter("model" if v.input_tokens else "rule" for _, v in rows)}
            print(f"#{p['fixer']:<4} <- #{p['breaker']:<4} {p['slug'].split('/')[-1]:<15} findings={len(findings)} spans={len(rows)} "
                  f"verified: updated-by-breaker {len(in_breaker)}, predicted escapes {len(escapes)} (confirmed by fixer {len(esc_hit)}); "
                  f"review {len(rev)} (confirmed {len(rev_hit)}); fixer lines naming a changed object, unsurfaced {len(missed)}; "
                  f"naive grep {naive_true}/{len(naive)} true")
            for e in esc_hit[:5]:
                print(f"        ESCAPE-HIT  {e[0]}:{e[1]}  {e[2][:80]}")
            for e in esc_open[:5]:
                print(f"        ESCAPE-?    {e[0]}:{e[1]}  {e[2][:80]}")
            for m in missed[:5]:
                print(f"        MISSED      {m[0]}  {m[1][:80]}")
            if out:
                out.write(json.dumps(rec) + "\n")
    print(f"\n{dict(tally)}")


if __name__ == "__main__":
    main()
