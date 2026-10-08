#!/usr/bin/env python3
"""Rank aftershock per repository from the two benchmarks.

  history  (history_bench_all): spans, verified true/false, changed-but-unverified, rule share
  pairs    (pr_pair_score):     API-change pairs, verified consequences, predicted escapes (confirmed),
                                review, naive name-grep precision for comparison
Usage: rank.py --history <dir-of-jsonl> --pairs <scored.jsonl>"""
from __future__ import annotations

import argparse, collections, glob, json, os


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--history"); ap.add_argument("--pairs"); ap.add_argument("--confirmed", help="confirm_escapes.py output")
    ap.add_argument("--max-lines", type=int, default=3000, help="history commits changing more lines than this are bulk rewrites: reported apart")
    a = ap.parse_args()
    import subprocess
    _size = {}
    def size(repo, sha):
        if (repo, sha) not in _size:
            d = subprocess.run(["git", "-C", repo, "diff", "--shortstat", f"{sha}^1", sha], capture_output=True, text=True).stdout
            n = 0
            for part in d.split(","):
                w = part.strip().split()
                if len(w) >= 2 and w[1].startswith(("insertion", "deletion")):
                    n += int(w[0])
            _size[(repo, sha)] = n
        return _size[(repo, sha)]
    conf = collections.defaultdict(lambda: collections.Counter())
    for r in map(json.loads, open(a.confirmed)) if a.confirmed else []:
        conf[r["slug"].split("/")[-1]][r["status"]] += 1
    H = collections.defaultdict(lambda: collections.Counter()); BULK = collections.Counter()
    for f in glob.glob(os.path.join(a.history, "*.jsonl")) if a.history else []:
        repo = os.path.basename(f)[:-6]
        for r in map(json.loads, open(f)):
            bulk = size(r["repo"], r["sha"]) > a.max_lines
            c = BULK if bulk else H[repo]
            if r["status"] == "unsurfaced":
                c["unsurf"] += 1; c["_shas_" + r["sha"][:8]] = 1; continue
            c["spans"] += 1; c["rule"] += r["how"] == "rule"
            v = r["status"] == "breaks_consumer"; t = r["truth_touched"]
            c["tp"] += v and t; c["fp"] += v and not t
            c["rev_t"] += (r["status"] == "needs_review") and t; c["cmp_t"] += (r["status"] == "compatible") and t; c["nr_t"] += (r["status"] == "not_reached") and t
            c["review"] += r["status"] == "needs_review"
            c["_shas_" + r["sha"][:8]] = 1
    # pairs: one row per breaker (several fixers of one breaker repeat the same consequences)
    seen_breaker = set()
    P = collections.defaultdict(lambda: collections.Counter())
    for r in map(json.loads, open(a.pairs)) if a.pairs else []:
        c = P[r["slug"].split("/")[-1]]; c["api_pairs"] += 1
        if (r["slug"], r["breaker"]) in seen_breaker:
            continue
        seen_breaker.add((r["slug"], r["breaker"])); c["breakers"] += 1
        c["verified"] += len(r["verified_in_breaker"]) + len(r["escape_confirmed_by_fixer"]) + len(r["escape_unconfirmed"])
        c["by_breaker"] += len(r["verified_in_breaker"]); c["esc_conf"] += len(r["escape_confirmed_by_fixer"]); c["esc_open"] += len(r["escape_unconfirmed"])
        c["review"] += len(r["review"]); c["review_conf"] += len(r["review_confirmed_by_fixer"]); c["missed"] += len(r["fixer_lines_naming_changed_object_unsurfaced"])
        c["naive"] += r.get("naive_lines", 0); c["naive_true"] += r.get("naive_true", 0)
    repos = sorted(set(H) | set(P), key=lambda k: -(H[k]["spans"] + P[k]["verified"]))
    print(f"{'repo':<20}{'commits':>8}{'spans':>6}{'verified':>9}{'false':>6}{'unsurf':>7}{'rev_t':>6}{'cmp_t':>6}{'rule%':>6} | {'breakers':>8}{'verified':>9}{'escapes':>8}{'review':>7}{'missed':>7} {'aftershock prec':>9} {'grep prec':>11} | {'strong/weak/still':>17}")
    T = collections.Counter(); TP = collections.Counter()
    for k in repos:
        h, p = H[k], P[k]
        shas = sum(1 for x in h if x.startswith("_shas_"))
        rule = f"{100 * h['rule'] / h['spans']:.0f}" if h["spans"] else "-"
        cprec = f"{100 * (p['by_breaker'] + p['esc_conf']) / max(1, p['verified']):.0f}%" if p["verified"] else "-"
        gprec = f"{100 * p['naive_true'] / p['naive']:.0f}% ({p['naive']})" if p["naive"] else "-"
        c = conf[k]
        print(f"{k:<20}{shas:>8}{h['spans']:>6}{h['tp']:>9}{h['fp']:>6}{h['unsurf']:>7}{h['rev_t']:>6}{h['cmp_t']:>6}{rule:>6} | {p['breakers']:>8}{p['verified']:>9}{p['esc_conf']:>4}/{p['esc_open']:<3}{p['review']:>7}{p['missed']:>7} {cprec:>9} {gprec:>11} | {c['confirmed']:>5} / {c['weak']:<3} / {c['still_present'] + c['rewritten_untracked']:<3}")
        T.update({x: h[x] for x in ("spans", "tp", "fp", "unsurf", "rev_t", "cmp_t", "rule")}); T["shas"] += shas; TP.update(p)
    print(f"{'TOTAL (ordinary)':<20}{T['shas']:>8}{T['spans']:>6}{T['tp']:>9}{T['fp']:>6}{T['unsurf']:>7}{T['rev_t']:>6}{T['cmp_t']:>6}{(100 * T['rule'] / max(1, T['spans'])):>5.0f} | {TP['breakers']:>8}{TP['verified']:>9}{TP['esc_conf']:>4}/{TP['esc_open']:<3}{TP['review']:>7}{TP['missed']:>7} "
          f"{100 * (TP['by_breaker'] + TP['esc_conf']) / max(1, TP['verified']):>8.0f}% {100 * TP['naive_true'] / max(1, TP['naive']):>10.0f}% ({TP['naive']})")
    b = BULK; bs = sum(1 for x in b if x.startswith("_shas_"))
    print(f"{'bulk rewrites (>' + str(a.max_lines) + ' lines), reported apart':<20}\n{'':<20}{bs:>8}{b['spans']:>6}{b['tp']:>9}{b['fp']:>6}{b['unsurf']:>7}{b['rev_t']:>6}{b['cmp_t']:>6}{(100 * b['rule'] / max(1, b['spans'])):>5.0f}")
    print("\nhistory: verified = aftershock said breaks AND the commit changed that line (or its import); false = said breaks, commit did not touch it;"
          "\n         unsurf = changed lines naming a removed object aftershock never surfaced; rev_t / cmp_t = changed lines aftershock sent to review / called compatible."
          "\npairs:   escapes a/b = verified lines the breaker did NOT update: a confirmed by the later fixer, b unconfirmed;"
          "\n         aftershock prec = share of verified consequences that were real (updated by breaker or fixer); grep prec = same for every line naming a changed object.")


if __name__ == "__main__":
    main()
