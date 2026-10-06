#!/usr/bin/env python3
"""Build the consumer-reach test set from httpx's own tests and docs.

Removed in httpx 0.28.0: the `app` and `proxies` parameters of Client/AsyncClient and of the
module-level request functions. The same keywords remain valid on other APIs (ASGITransport(app=),
WSGITransport(app=), mounts/proxy dicts), so a token grep cannot tell them apart. Labels here come
from the AST (callee name), not from a model.

Usage: build_cases.py <httpx-checkout> <tag> [<tag>...]   -> spike/cases/httpx.jsonl
"""
import ast, json, pathlib, re, subprocess, sys

REMOVED = {"app", "proxies"}
CLIENT_CALLEES = {"Client", "AsyncClient", "request", "stream", "get", "post", "put", "patch",
                  "delete", "head", "options"}
ROOT = pathlib.Path(__file__).resolve().parent
out = ROOT / "cases" / "httpx.jsonl"


def callee_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return "?"


def git_show(repo, ref, path):
    return subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True, text=True).stdout


def git_files(repo, ref, kw):
    r = subprocess.run(["git", "-C", repo, "grep", "-l", "-E", f"{kw}=", ref, "--", "tests", "docs"],
                       capture_output=True, text=True)
    return [line.split(":", 1)[1] for line in r.stdout.splitlines() if ":" in line]


def cases_from_py(src, path, ref):
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kws = {k.arg for k in node.keywords if k.arg in REMOVED}
        if not kws:
            continue
        name = callee_name(node)
        seg = ast.get_source_segment(src, node) or ""
        lo = max(0, node.lineno - 4)
        context = "\n".join(lines[lo:node.end_lineno])
        for kw in kws:
            yield dict(id=f"{ref}:{path}:{node.lineno}:{kw}", ref=ref, path=path, line=node.lineno,
                       keyword=kw, callee=name, label=name in CLIENT_CALLEES, code=seg, context=context,
                       source="py")


def cases_from_md(src, path, ref):
    """Docs: regex over each fenced code statement; callee = last `Name(` before the keyword."""
    lines = src.splitlines()
    for i, line in enumerate(lines):
        for kw in REMOVED:
            if re.search(rf"\b{kw}=", line):
                stmt = "\n".join(lines[max(0, i - 3):i + 1])
                m = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\(", stmt)
                name = m[-1] if m else "?"
                yield dict(id=f"{ref}:{path}:{i+1}:{kw}", ref=ref, path=path, line=i + 1, keyword=kw,
                           callee=name, label=name in CLIENT_CALLEES, code=line.strip(),
                           context="\n".join(lines[max(0, i - 4):i + 2]), source="md")


def main():
    repo, refs = sys.argv[1], sys.argv[2:]
    seen, cases = set(), []
    for ref in refs:
        for kw in REMOVED:
            for path in git_files(repo, ref, kw):
                src = git_show(repo, ref, path)
                gen = cases_from_py if path.endswith(".py") else cases_from_md
                try:
                    for c in gen(src, path, ref):
                        key = (c["path"], c["keyword"], c["code"])
                        if key in seen:      # same snippet at both tags -> one case
                            continue
                        seen.add(key); cases.append(c)
                except SyntaxError:
                    pass
    out.parent.mkdir(exist_ok=True)
    with out.open("w") as f:
        for c in cases:
            f.write(json.dumps(c) + "\n")
    pos = sum(c["label"] for c in cases)
    print(f"{len(cases)} cases -> {out}  positives={pos} negatives={len(cases)-pos}")
    by = {}
    for c in cases:
        by.setdefault((c["keyword"], c["callee"], c["label"]), 0); by[(c["keyword"], c["callee"], c["label"])] += 1
    for k, v in sorted(by.items(), key=lambda kv: -kv[1]):
        print(f"  {v:3d}  {k[0]}= -> {k[1]}()  label={k[2]}")


if __name__ == "__main__":
    main()
