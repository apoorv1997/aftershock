#!/usr/bin/env python3
"""Contract-break cases from click 8.1.8 -> 8.2.0 (griffe findings x click's own consumers).

Label rules (by construction, no model):
  removed public object referenced as click's           -> breaks_consumer
  progressbar(...) with >=4 positional args              -> breaks_consumer (params became keyword-only)
  progressbar(...) otherwise                             -> compatible
  get_metavar/get_missing_message/make_metavar override
      without a `ctx` parameter                          -> breaks_consumer (ctx added as required)
      with `ctx`                                         -> compatible
Usage: build_click_cases.py <click-checkout> <old-tag>  -> spike/cases/click.jsonl
"""
import ast, json, pathlib, re, subprocess, sys

REMOVED = {"BaseCommand": "click.core.BaseCommand: Public object was removed",
           "MultiCommand": "click.core.MultiCommand: Public object was removed",
           "OptionParser": "click.parser.OptionParser: Public object was removed",
           "split_arg_string": "click.parser.split_arg_string: Public object was removed",
           "split_opt": "click.parser.split_opt: Public object was removed",
           "normalize_opt": "click.parser.normalize_opt: Public object was removed",
           "ParsingState": "click.parser.ParsingState: Public object was removed"}
PB = "click.termui.progressbar: show_eta, show_percent, show_pos, item_show_func, fill_char, empty_char, bar_template, info_sep, width, file, color, update_min_steps: positional parameters became keyword-only"
CTX = {"get_metavar": "ParamType.get_metavar(ctx): Parameter was added as required",
       "get_missing_message": "ParamType.get_missing_message(ctx): Parameter was added as required",
       "make_metavar": "Parameter.make_metavar(ctx): Parameter was added as required"}
ROOT = pathlib.Path(__file__).resolve().parent
out = ROOT / "cases" / "click.jsonl"


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout


def py_cases(src, path, ref):
    tree = ast.parse(src); lines = src.splitlines()
    click_names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("click"):
            click_names |= {a.asname or a.name for a in n.names}
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in REMOVED and isinstance(n.value, ast.Name) and n.value.id == "click" \
           or isinstance(n, ast.Name) and n.id in REMOVED and n.id in click_names:
            name = n.attr if isinstance(n, ast.Attribute) else n.id
            yield case(ref, path, n.lineno, REMOVED[name], "breaks_consumer", lines)
        elif isinstance(n, ast.Call):
            f = n.func; fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if fname == "progressbar":
                lab = "breaks_consumer" if len(n.args) >= 4 else "compatible"
                yield case(ref, path, n.lineno, PB, lab, lines, n.end_lineno)
        elif isinstance(n, ast.FunctionDef) and n.name in CTX:
            params = [a.arg for a in n.args.args]
            lab = "compatible" if "ctx" in params else "breaks_consumer"
            yield case(ref, path, n.lineno, CTX[n.name], lab, lines, n.end_lineno)


def md_cases(src, path, ref):
    lines = src.splitlines()
    for i, line in enumerate(lines):
        for name, finding in REMOVED.items():
            if re.search(rf"\bclick\.{name}\b|\bimport .*\b{name}\b", line):
                yield case(ref, path, i + 1, finding, "breaks_consumer", lines)


def case(ref, path, lineno, finding, label, lines, end=None):
    end = end or lineno
    ctx = "\n".join(lines[max(0, lineno - 4):end])
    return dict(id=f"{ref}:{path}:{lineno}:{label}", ref=ref, path=path, line=lineno, finding=finding,
                label=label, code="\n".join(lines[lineno - 1:end]), context=ctx)


def main():
    repo, ref = sys.argv[1], sys.argv[2]
    files = [f for f in git(repo, "ls-tree", "-r", "--name-only", ref).splitlines()
             if f.startswith(("tests/", "docs/", "examples/")) and f.endswith((".py", ".md", ".rst"))]
    seen, cases = set(), []
    for path in files:
        src = git(repo, "show", f"{ref}:{path}")
        try:
            gen = py_cases(src, path, ref) if path.endswith(".py") else md_cases(src, path, ref)
            for c in gen:
                k = (c["path"], c["code"], c["finding"])
                if k not in seen: seen.add(k); cases.append(c)
        except SyntaxError:
            pass
    with out.open("w") as f:
        for c in cases: f.write(json.dumps(c) + "\n")
    print(f"{len(cases)} cases -> {out}")
    tally = {}
    for c in cases: tally[(c["finding"][:40], c["label"])] = tally.get((c["finding"][:40], c["label"]), 0) + 1
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]): print(f"  {v:3d}  {k[0]:40s} {k[1]}")


if __name__ == "__main__":
    main()
