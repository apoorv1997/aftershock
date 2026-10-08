#!/usr/bin/env python3
"""Fix benchmark: for every TRUE verified consequence of a history-benchmark run, propose fixes as if the commit had
missed that file (leave-one-out: the commit's diff WITHOUT the file is all a generator may learn from), climb the
fixcheck ladder, and compare with what the author actually wrote on those lines.

Usage: fix_bench.py <lib-repo> <package> --rows spike/pub-<x>-m.jsonl [--search src] [--python ENV_PY]
                    [--out spike/fix-<x>.jsonl] [--limit N]
Without --python the 'runs' rung is not attempted (a library's dependencies are rarely in this environment)."""
from __future__ import annotations

import argparse, collections, difflib, json, pathlib, re, subprocess, sys, tempfile

from cie import contracts, fix, fixcheck, reach


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def author_block(old_src: str, new_src: str, lo: int, hi: int) -> str | None:
    """What the author's version holds in place of old lines lo..hi (1-based), or None if those lines are untouched."""
    a, b = old_src.splitlines(), new_src.splitlines()
    out, touched = [], False
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if i2 < lo or i1 >= hi:            # opcode entirely outside lo..hi (0-based i1..i2 vs 1-based lo..hi)
            if not (tag == "insert" and lo - 1 <= i1 <= hi):
                continue
        if tag != "equal":
            touched = True
        out += b[j1:j2] if tag != "equal" else a[max(i1, lo - 1):min(i2, hi)]
    return "\n".join(out) if touched else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lib"); ap.add_argument("package"); ap.add_argument("--rows", required=True)
    ap.add_argument("--search"); ap.add_argument("--python"); ap.add_argument("--out"); ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.rows)]
    rows = [r for r in rows if r["status"] == "breaks_consumer" and r["truth_touched"]][: a.limit]
    out = open(a.out or f"spike/fix-{pathlib.Path(a.lib).name}.jsonl", "w")
    findings_by_sha, tally = {}, collections.Counter()
    by_method = collections.defaultdict(collections.Counter)
    groups = collections.defaultdict(list)       # the unit is (commit, file): a missed caller needs EVERY change fixed
    for r in rows:
        groups[(r["sha"], r["file"])].append((r["line"], r["finding"]))
    for (sha, rel), items in groups.items():
        lines_ = sorted({ln for ln, _ in items})
        fdescs = {fd for _, fd in items}
        old, new = sha + "^", sha
        if sha not in findings_by_sha:
            findings_by_sha[sha] = contracts.griffe_findings(a.package, old, new, cwd=a.lib, search=a.search, timeout=300)
        fs = [f for f in findings_by_sha[sha] if f.describe() in fdescs]
        old_src = git(a.lib, "show", f"{old}:{rel}")
        new_src = git(a.lib, "show", f"{new}:{rel}")
        rec = {"sha": sha, "file": rel, "lines": lines_, "findings": sorted(fdescs)}
        if not fs or not old_src:
            rec["outcome"] = "no finding / file"; tally[rec["outcome"]] += 1; out.write(json.dumps(rec) + "\n"); continue
        if any(rel == f.path for f in fs):   # a module that defines a changed object: its edit IS the change, not a missed caller
            rec["outcome"] = "defines the changed object (not scored)"; tally[rec["outcome"]] += 1
            out.write(json.dumps(rec) + "\n"); continue
        with tempfile.TemporaryDirectory(prefix="cie-fb-") as d:
            p = pathlib.Path(d, rel); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(old_src)
            spans = [s for s in reach.candidates(d, fs, exports=contracts.package_exports(a.lib, a.package, old, a.search),
                                                 modules=contracts.library_modules(a.lib, a.package, old, a.search))
                     if (s.line, s.finding.describe()) in set(items)]
            if not spans:
                rec["outcome"] = "span not reproduced"; tally[rec["outcome"]] += 1; out.write(json.dumps(rec) + "\n"); continue
            diff = git(a.lib, "diff", "-U0", old, new, "--", ".", f":!{rel}")        # leave-one-out: not this file
            rec["fixes"] = []
            for fx in fix.propose_file(spans, old_src, a.lib, a.package, a.search, old, new, diff):
                fc = fixcheck.check(old_src, rel, fx.edits, target=fs, findings=fs, lib_root=a.lib, package=a.package,
                                    old_ref=old, new_ref=new, search=a.search, consumer_root=d, python=a.python,
                                    run=bool(a.python))
                verdicts = []
                for e in fx.edits:
                    truth = author_block(old_src, new_src, e.start, e.end)
                    verdicts.append("author left these lines" if truth is None else (
                        "matches author" if norm(truth) == norm(e.text) else ("author deleted" if not truth.strip() else "differs")))
                match = verdicts[0] if len(set(verdicts)) == 1 else "partly matches author" if "matches author" in verdicts else "differs"
                rec["fixes"].append({"method": fx.method, "exact": fx.exact, "note": fx.note[:200], "edits": len(fx.edits),
                                     "text": [e.text[:160] for e in fx.edits][:4], "level": fc.level, "summary": fc.summary(),
                                     "behaviour_change": fc.behaviour_change, "match": match})
                by_method[fx.method][f"level {fc.level}"] += 1
                by_method[fx.method][match] += 1
        rec["outcome"] = "fixed (held up to runs)" if any(x["level"] == "runs" and not x["behaviour_change"] for x in rec["fixes"]) else \
            ("fixed statically (clears)" if any(x["level"] == "clears" for x in rec["fixes"]) else
             ("proposed, did not hold up" if rec["fixes"] else "no proposal"))
        tally[rec["outcome"]] += 1
        out.write(json.dumps(rec) + "\n"); out.flush()
    print(f"{len(rows)} true consequences in {len(groups)} (commit, file) units: " + ", ".join(f"{k} {v}" for k, v in tally.most_common()))
    held = [x for l in open(out.name) for x in json.loads(l).get("fixes", []) if x["level"] == "runs" and not x["behaviour_change"]]
    print(f"  of {len(held)} fixes that held up to runs: " + ", ".join(f"{k} {v}" for k, v in collections.Counter(x["match"] for x in held).most_common()))
    for m, c in by_method.items():
        print(f"  {m:8} " + ", ".join(f"{k}: {v}" for k, v in sorted(c.items())))


if __name__ == "__main__":
    main()
