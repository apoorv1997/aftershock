#!/usr/bin/env python3
"""Confirm predicted escapes forward in history, independent of PR-pair linking.

For every verified consequence the breaker did not update, find the first later first-parent commit that
rewrote or removed that exact statement (git pickaxe on the statement text, breaker..ref, that file).
Otherwise report whether the statement is still present at ref (an unfixed latent break, or dead code).
Usage: confirm_escapes.py <scored.jsonl> --repo <slug>=<path> [--repo ...] [--ref origin/main] [--out confirmed.jsonl]"""
from __future__ import annotations

import argparse, datetime, json, re, subprocess


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("scored"); ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--ref", default="HEAD"); ap.add_argument("--pairs", help="pairs.jsonl for breaker shas"); ap.add_argument("--out")
    a = ap.parse_args()
    repos = dict(r.split("=", 1) for r in a.repo)
    shas = {(p["slug"], p["breaker"]): (p["breaker_sha"], p["breaker_merged"]) for p in map(json.loads, open(a.pairs))} if a.pairs else {}
    out = open(a.out, "w") if a.out else None
    seen = set(); tally = {"confirmed": 0, "weak": 0, "still_present": 0, "file_gone": 0}
    for r in map(json.loads, open(a.scored)):
        if r["slug"] not in repos or not r["escape_unconfirmed"]:
            continue
        repo = repos[r["slug"]]; bsha, bdate = shas.get((r["slug"], r["breaker"]), (None, None))
        if not bsha:
            continue
        ref = a.ref if git(repo, "rev-parse", "--verify", "-q", a.ref).strip() else "HEAD"
        for f, ln, stmt in r["escape_unconfirmed"]:
            k = (r["slug"], r["breaker"], f, stmt)
            if k in seen or not stmt:
                continue
            seen.add(k)
            hit = git(repo, "log", "--first-parent", "--format=%H|%ad|%s", "--date=short", f"-S{stmt}", f"{bsha}..{ref}", "--", f).strip().splitlines()
            rec = {"slug": r["slug"], "breaker": r["breaker"], "file": f, "line": ln, "stmt": stmt[:120], "breaker_merged": bdate}
            if hit:
                csha, cdate, subj = hit[-1].split("|", 2)      # oldest later commit that changed the statement's count
                days = (datetime.date.fromisoformat(cdate) - datetime.date.fromisoformat(bdate)).days if bdate else None
                nfiles = len(git(repo, "diff", "--name-only", f"{csha}^1", csha).splitlines())
                # a targeted commit rewriting the flagged statement is strong evidence; a release / sync merge that
                # rewrote hundreds of files touches the statement for reasons of its own
                strong = nfiles <= 30 and not re.search(r"release|merge (development|main|dev)|sync|integration", subj, re.I)
                rec.update(status="confirmed" if strong else "weak", fixed_by=csha[:8], fixed_on=cdate, fixed_subject=subj[:70],
                           days_later=days, fixer_files=nfiles); tally["confirmed" if strong else "weak"] += 1
            else:
                head_src = git(repo, "show", f"{ref}:{f}")
                if head_src:
                    rec.update(status="still_present" if stmt in head_src else "rewritten_untracked"); tally["still_present"] += 1
                else:
                    rec.update(status="file_gone"); tally["file_gone"] += 1
            print(f"{r['slug'].split('/')[-1]:16s} #{r['breaker']:<4} {f.split('/')[-1]}:{ln:<5} {rec['status']:14s} "
                  f"{(rec.get('fixed_by','') + ' +' + str(rec.get('days_later','')) + 'd ' + str(rec.get('fixer_files','')) + 'f  ' + rec.get('fixed_subject','')[:45]) if hit else ''}  | {stmt[:50]}")
            if out:
                out.write(json.dumps(rec) + "\n")
    print(f"\n{tally}")


if __name__ == "__main__":
    main()
