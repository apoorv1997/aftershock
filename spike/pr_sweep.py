#!/usr/bin/env python3
"""Every open PR of every repository in the estate manifest, one pr-check each; a table of verdicts.
With --only-breaks the comment policy is applied: post where something breaks, remove ours where nothing does.
Usage: pr_sweep.py [--estate ...] [--only-breaks] [--repos a,b] [--out sweep.md]"""
from __future__ import annotations

import argparse, json, os, re, subprocess, sys, time


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--estate", default="~/.config/cie/estate.json"); ap.add_argument("--only-breaks", action="store_true")
    ap.add_argument("--repos"); ap.add_argument("--out"); ap.add_argument("--cie", default=os.path.join(os.path.dirname(sys.executable), "cie"))
    a = ap.parse_args(); est = json.load(open(os.path.expanduser(a.estate)))
    names = a.repos.split(",") if a.repos else [n for n, r in est["repos"].items() if not r.get("ext")]
    rows = []
    for name in names:
        slug = est["repos"][name]["slug"]
        prs = json.loads(subprocess.run(["gh", "pr", "list", "-R", slug, "--state", "open", "--limit", "100", "--json", "number,title,updatedAt,isDraft"], capture_output=True, text=True).stdout or "[]")
        for pr in sorted(prs, key=lambda p: p["number"]):
            t0 = time.time()
            r = subprocess.run([a.cie, "pr-check", "--estate", a.estate, "--repo", name, "--pr", str(pr["number"])] + (["--only-breaks"] if a.only_breaks else []), capture_output=True, text=True)
            out = r.stdout + r.stderr
            v = re.search(r"\[verdict\] (\w+) breaks=(\d+) looks=(\d+) checks=(\d+)", out)
            action = "posted" if "[posted]" in out else "updated" if "[updated" in out else "removed earlier" if "[removed earlier" in out else ""
            first_break = next((l.strip("- ").strip() for l in out.splitlines() if l.startswith("- `") and "—" in l), "")
            row = {"repo": name, "pr": pr["number"], "title": pr["title"][:60], "draft": pr["isDraft"], "verdict": v.group(1) if v else ("error" if r.returncode else "no surface"),
                   "breaks": int(v.group(2)) if v else 0, "looks": int(v.group(3)) if v else 0, "checks": int(v.group(4)) if v else 0, "action": action, "first": first_break[:120], "seconds": round(time.time() - t0)}
            rows.append(row)
            print(f"{name:<16}#{pr['number']:<5}{row['verdict']:<11}breaks={row['breaks']:<3}looks={row['looks']:<3}checks={row['checks']:<3}{action:<16}{pr['title'][:50]}", flush=True)
    if a.out:
        with open(a.out, "w") as f:
            f.write("| repo | PR | verdict | breaks | look at | checks | comment | title |\n|---|---|---|---|---|---|---|---|\n")
            for r in rows:
                f.write(f"| {r['repo']} | #{r['pr']} | {r['verdict']} | {r['breaks']} | {r['looks']} | {r['checks']} | {r['action'] or '—'} | {r['title']} |\n")
            f.write("\n"); f.write("\n".join(json.dumps(r) for r in rows))
    print(f"\n{len(rows)} PRs: {sum(1 for r in rows if r['verdict']=='break')} break, {sum(1 for r in rows if r['verdict']=='look')} to look at, {sum(1 for r in rows if r['verdict']=='clean')} clean, {sum(1 for r in rows if r['verdict'] in ('unchecked', 'error'))} UNCHECKED, {sum(1 for r in rows if r['verdict']=='no surface')} without a surface")


if __name__ == "__main__":
    main()
