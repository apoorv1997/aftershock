#!/usr/bin/env python3
"""Derive library -> consumer lists from the code instead of a hand-written manifest, and diff the two.

For every Python package under the library repository's `python/*/src/<pkg>` (and any manifest library), grep
every manifest repository at its default branch for `import <pkg>` / `from <pkg>` in non-test .py files.
Usage: derive_consumers.py --estate ~/.config/aftershock/estate.json [--write]"""
from __future__ import annotations

import argparse, json, os, re, subprocess


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--estate", default="~/.config/aftershock/estate.json"); ap.add_argument("--write", action="store_true")
    a = ap.parse_args(); path = os.path.expanduser(a.estate); est = json.load(open(path))
    repos = {k: {**v, "path": os.path.expanduser(v["path"])} for k, v in est["repos"].items()}
    # library packages: every python/*/src/<pkg> in any repo, plus what the manifest already lists
    libs = {(l["repo"], l["package"], l["search"]) for l in est.get("libraries", [])}
    for name, r in repos.items():
        git(r["path"], "fetch", "-q", "origin", r["default"])
        for f in git(r["path"], "ls-tree", "-r", "--name-only", f"origin/{r['default']}").splitlines():
            m = re.match(r"^(python/[^/]+/src)/([A-Za-z_]\w*)/__init__\.py$", f)
            if m:
                libs.add((name, m.group(2), m.group(1)))
    found = {}
    for lib_repo, pkg, search in sorted(libs):
        consumers = set()
        for name, r in repos.items():
            if r.get("ext"):                      # not a Python consumer
                continue
            # POSIX ERE (git grep -E): no \s, no \b
            hits = git(r["path"], "grep", "-lE", rf"^[[:space:]]*(from|import)[[:space:]]+{pkg}([[:space:]]|\.|$)", f"origin/{r['default']}", "--", "*.py").splitlines()
            # tests never count; the library's OWN package dir only in its own repo (every public repo has a src/)
            hits = [h for h in hits if not re.search(r"(^|/)tests?/|/test_", h)
                    and not (name == lib_repo and re.search(r"^origin/[^:]+:" + re.escape(f"{search}/{pkg}/" if search else f"{pkg}/"), h))]
            # manifest "consumer_ignore": directory names whose imports are examples or tooling, not consumers (public repos: docs_src, scripts)
            ign = est.get("consumer_ignore", [])
            hits = [h for h in hits if not any(f"/{d}/" in "/" + h.split(":", 1)[1] for d in ign)]
            if hits:
                consumers.add(name)
        found[(lib_repo, pkg, search)] = consumers
    print(f"{'library':<32}{'manifest consumers':<48}derived from imports")
    changed = False
    for (lib_repo, pkg, search), cons in found.items():
        entry = next((l for l in est.get("libraries", []) if l["repo"] == lib_repo and l["package"] == pkg), None)
        listed = set(entry["consumers"]) if entry else set()
        flag = "" if listed == cons else ("  <-- MISSING " + ",".join(sorted(cons - listed)) if cons - listed else "") + ("  <-- STALE " + ",".join(sorted(listed - cons)) if listed - cons else "")
        print(f"{lib_repo + ':' + pkg:<32}{', '.join(sorted(listed)) or '-':<48}{', '.join(sorted(cons)) or '-'}{flag}")
        if a.write and cons and listed != cons:
            if entry:
                entry["consumers"] = sorted(cons)
            else:
                est.setdefault("libraries", []).append({"repo": lib_repo, "package": pkg, "search": search, "consumers": sorted(cons)})
            changed = True
    if a.write and changed:
        json.dump(est, open(path, "w"), indent=2); print(f"\nmanifest updated: {path}")


if __name__ == "__main__":
    main()
