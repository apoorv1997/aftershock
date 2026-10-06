#!/usr/bin/env python3
"""Mine (breaker PR, fixer PR) pairs from a repository's merged pull requests.

Truth for the history benchmark is "lines the same commit changed" — callers the author did update.
This finds the other kind: a later PR that repairs what an earlier PR broke. Two linkers:
  ref    the fixer's title/body cites the earlier PR (#N) and reads like a fix / revert / follow-up
  blame  the fixer is a fix-type PR whose removed lines were written by one recent earlier PR
Output: one JSON line per pair {fixer, breaker, kind, evidence}; pass --out. Needs `gh` and a local clone
with the merge commits fetched. Results name private PRs — keep them out of the public repo."""
from __future__ import annotations

import argparse, collections, json, re, subprocess

FIXY = re.compile(r"\b(fix|hotfix|regress\w*|broke\w*|revert\w*|follow[- ]?up|repair|restore)\b", re.I)
SUBJECT_PR = re.compile(r"\(#(\d+)\)|^Merge pull request #(\d+)")


def sh(*a, cwd=None):
    return subprocess.run(a, capture_output=True, text=True, cwd=cwd).stdout


def merged_prs(slug, limit):
    out = sh("gh", "pr", "list", "-R", slug, "--state", "merged", "--limit", str(limit),
             "--json", "number,title,body,mergedAt,mergeCommit,baseRefName")
    prs = {p["number"]: p for p in json.loads(out or "[]") if p.get("mergeCommit")}
    for p in prs.values():
        p["sha"] = p["mergeCommit"]["oid"]
    return prs


def removed_ranges(repo, sha, exts=(".py",)):
    """Old-side (file, start, end) ranges the PR's merge commit removed or rewrote (first-parent diff)."""
    diff = sh("git", "diff", "-U0", f"{sha}^1", sha, cwd=repo)
    cur, out = None, []
    for l in diff.splitlines():
        if l.startswith("--- a/"):
            cur = l[6:] if l.endswith(tuple(exts)) else None   # code only: a 15k-line JSON data file blamed hunk by hunk took hours
        elif l.startswith("--- /dev/null"):
            cur = None
        m = re.match(r"@@ -(\d+)(?:,(\d+))? ", l)
        if m and cur:
            n = int(m.group(2) or 1)
            if n:
                out.append((cur, int(m.group(1)), int(m.group(1)) + n - 1))
    return out


_SUBJ: dict[str, str] = {}
_PR_OF: dict[str, dict[str, int]] = {}


def pr_of_commit(repo):
    """sha -> PR number for every commit a first-parent merge brought in (merge workflows have no `(#N)`
    on the branch commits; squash workflows have it on the one commit)."""
    if repo in _PR_OF:
        return _PR_OF[repo]
    m = {}
    for line in sh("git", "log", "--first-parent", "--merges", "--format=%H %P %s", cwd=repo).splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 4:
            continue
        msha, p1, p2, subj = parts[0], parts[1], parts[2], parts[3]
        g = SUBJECT_PR.search(subj)
        if not g:
            continue
        n = int(g.group(1) or g.group(2))
        for csha in sh("git", "rev-list", f"{p1}..{p2}", cwd=repo).split():
            m.setdefault(csha, n)
        m.setdefault(msha, n)
    _PR_OF[repo] = m
    return m


def blame_prs(repo, sha, ranges):
    """PR number -> number of removed lines it originally wrote (blamed at the fixer's first parent)."""
    c = collections.Counter()
    pr_of = pr_of_commit(repo)
    by_file = collections.defaultdict(list)
    for f, a, b in ranges:
        by_file[f].append(f"{a},{b}")
    for f, ls in by_file.items():                        # one blame per file, every range at once
        args = [x for r in ls[:200] for x in ("-L", r)]
        for l in sh("git", "blame", "-l", *args, f"{sha}^1", "--", f, cwd=repo).splitlines():
            csha = l.split(" ", 1)[0].lstrip("^")
            if csha in pr_of:
                c[pr_of[csha]] += 1
                continue
            if csha not in _SUBJ:                       # one subject lookup per commit, not per line
                _SUBJ[csha] = sh("git", "log", "-1", "--format=%s", csha, cwd=repo)
            m = SUBJECT_PR.search(_SUBJ[csha])
            if m:
                c[int(m.group(1) or m.group(2))] += 1
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo"); ap.add_argument("slug")
    ap.add_argument("--limit", type=int, default=300); ap.add_argument("--min-lines", type=int, default=3)
    ap.add_argument("--max-days", type=int, default=120); ap.add_argument("--out")
    ap.add_argument("--ext", default=".py", help="comma-separated source extensions the blame linker reads (.ts,.tsx for a UI, .yaml,.tpl for a chart)")
    a = ap.parse_args()
    prs = merged_prs(a.slug, a.limit)
    print(f"{len(prs)} merged PRs with a merge commit in {a.slug}")
    pairs = []
    for n, p in sorted(prs.items()):
        text = f"{p['title']}\n{p.get('body') or ''}"
        fixy = bool(FIXY.search(p["title"])) or bool(re.search(r"\b(regress|broke|broken|revert)", text, re.I))
        for r in {int(x) for x in re.findall(r"#(\d+)", text)} - {n}:
            # a PR merged into a feature branch that this PR later merged is a stacked step, not a breaker
            if r in prs and prs[r]["mergedAt"] < p["mergedAt"] and fixy and prs[r]["baseRefName"] == p["baseRefName"]:
                pairs.append({"fixer": n, "breaker": r, "kind": "ref", "evidence": "cited in a fix-type PR"})
        if not FIXY.search(p["title"]):
            continue
        ranges = removed_ranges(a.repo, p["sha"], tuple(x.strip() for x in a.ext.split(",")))
        if not ranges:
            continue
        for r, lines in blame_prs(a.repo, p["sha"], ranges).most_common(2):
            if r == n or r not in prs or prs[r]["mergedAt"] >= p["mergedAt"] or lines < a.min_lines:
                continue
            if prs[r]["baseRefName"] != p["baseRefName"]:
                continue
            days = (int(p["mergedAt"][8:10]) - int(prs[r]["mergedAt"][8:10])) + 31 * (int(p["mergedAt"][5:7]) - int(prs[r]["mergedAt"][5:7]))
            if days > a.max_days:
                continue
            pairs.append({"fixer": n, "breaker": r, "kind": "blame", "evidence": f"{lines} removed lines written by it"})
    seen = set(); uniq = []
    for q in pairs:
        k = (q["fixer"], q["breaker"])
        if k not in seen:
            seen.add(k); uniq.append(q)
    for q in uniq:
        f, b = prs[q["fixer"]], prs[q["breaker"]]
        q.update(fixer_title=f["title"][:70], breaker_title=b["title"][:70], fixer_sha=f["sha"], breaker_sha=b["sha"],
                 fixer_merged=f["mergedAt"][:10], breaker_merged=b["mergedAt"][:10], slug=a.slug)
        print(f"  #{q['fixer']:<4} {q['fixer_merged']} {q['fixer_title'][:48]:<48} <- #{q['breaker']:<4} {q['breaker_merged']} {q['breaker_title'][:40]:<40} [{q['kind']}: {q['evidence']}]")
    print(f"{len(uniq)} pairs")
    if a.out:
        with open(a.out, "a") as fh:
            for q in uniq:
                fh.write(json.dumps(q) + "\n")


if __name__ == "__main__":
    main()
