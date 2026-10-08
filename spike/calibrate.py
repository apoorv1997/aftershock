#!/usr/bin/env python3
"""Calibrate the reach/break thresholds on rule-labelled evidence.

Ground truth without a model:
  removed parameter   -> reach = the call's callee is the finding's object; break = reach
  removed object      -> reach = the reference is the library's object; break = reach
  positional move     -> reach = callee matches; break = some positional arg now lands on a different
                         parameter (old vs new signature, parsed from the library at both tags)
Evidence variants: `plain` (3 lines of context) and `truncated` (only the line holding the keyword or
name, callee often invisible) — the second exists to populate the 0.2-0.8 band.

Usage: calibrate.py <lib-checkout> <package> <old> <new> <consumer-tree> [--search src] [--library-name httpx]
Writes spike/calibration-<package>.jsonl and prints reliability tables + recommended thresholds."""
from __future__ import annotations

import argparse, ast, json, pathlib, subprocess, sys
from aftershock import contracts, evidence, reach, verify

ROOT = pathlib.Path(__file__).resolve().parent


def git_show(repo, ref, path):
    return subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True, text=True).stdout


def signature(repo, ref, path, object_name):
    """Positional parameter names (excluding self) of `object_name` at `ref`, or None."""
    src = git_show(repo, ref, path)
    if not src:
        return None
    parts = object_name.split(".")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    nodes = list(ast.walk(tree))
    if len(parts) == 2:  # Class.method
        for cls in nodes:
            if isinstance(cls, ast.ClassDef) and cls.name == parts[0]:
                for fn in cls.body:
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == parts[1]:
                        return [a.arg for a in fn.args.posonlyargs + fn.args.args if a.arg != "self"]
    else:
        for fn in nodes:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == parts[-1]:
                return [a.arg for a in fn.args.posonlyargs + fn.args.args if a.arg != "self"]
    return None


def label(span, lib, old, new, pkg_prefix, exports):
    """(reach, break) by rule, or None when the rule cannot decide."""
    f = span.finding
    if "Public object was removed" in f.message:
        # resolve the reference through the consumer file's imports + the library's re-exports
        consumer_src = pathlib.Path(sys.argv[5]).joinpath(span.file).read_text(errors="replace")
        imports = reach._import_map(ast.parse(consumer_src))
        try:
            stmt = ast.parse(span.code.strip()).body[0]
        except (SyntaxError, IndexError):
            return None
        target = f.module + "." + f.object_name.split(".")[0]
        hit = False
        for n in ast.walk(stmt):
            d = reach._dotted(n)
            if d and d.split(".")[-1] == f.object_name.split(".")[-1]:
                hit = hit or reach.resolve(d, imports, exports) == target
        return (hit, hit)
    try:
        node = ast.parse(span.code.strip()).body[0]
    except (SyntaxError, IndexError):
        return None
    call = node.value if isinstance(node, ast.Expr) else getattr(node, "value", None)
    if isinstance(node, (ast.Assign, ast.AnnAssign)) and not isinstance(call, ast.Call):
        call = None
    if isinstance(node, ast.With):
        for item in node.items:
            if isinstance(item.context_expr, ast.Call):
                call = item.context_expr
    if not isinstance(call, ast.Call):
        return None
    callee = reach._callee(call)
    head = f.object_name.split(".")[0]
    is_obj = callee == head or callee == f.object_name.split(".")[-1]
    if "Parameter was removed" in f.message:
        kws = {k.arg for k in call.keywords}
        r = is_obj and f.parameter in kws
        return (r, r)
    if "Positional parameter was moved" in f.message:
        if not is_obj:
            return (False, False)
        so, sn = signature(lib, old, f.path, f.object_name), signature(lib, new, f.path, f.object_name)
        if so is None or sn is None:
            return None
        k = len(call.args)
        broken = any(i >= len(sn) or so[i] != sn[i] for i in range(min(k, len(so))))
        return (True, broken)
    if "Public object was removed" in f.message:
        return (is_obj, is_obj)
    return None


def truncated(span):
    """Only the line that carries the keyword/name: the callee is often on another line."""
    tok = span.finding.parameter + "=" if span.finding.parameter else span.finding.object_name.split(".")[-1]
    for line in span.context.splitlines():
        if tok in line:
            return line.strip()
    return span.code.splitlines()[-1].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lib"); ap.add_argument("package"); ap.add_argument("old"); ap.add_argument("new"); ap.add_argument("consumer")
    ap.add_argument("--search"); ap.add_argument("--library-name")
    a = ap.parse_args()
    name = a.library_name or a.package
    findings = contracts.griffe_findings(a.package, a.old, a.new, cwd=a.lib, search=a.search)
    exports = contracts.package_exports(a.lib, a.package, a.old, a.search)
    spans = reach.candidates(a.consumer, findings, exports=exports)
    # positional moves are decided deterministically in the engine; the model is calibrated on the rest
    spans = [s for s in spans if "Positional parameter was moved" not in s.finding.message]
    labelled = [(s, lab) for s in spans if (lab := label(s, a.lib, a.old, a.new, name, exports)) is not None]
    print(f"{len(findings)} findings, {len(spans)} candidates, {len(labelled)} rule-labelled "
          f"(reach+={sum(l[0] for _, l in labelled)}, break+={sum(l[1] for _, l in labelled)})")
    out = ROOT / f"calibration-{name}.jsonl"
    done = {(json.loads(l)["id"], json.loads(l)["variant"]) for l in out.read_text().splitlines()} if out.exists() else set()
    with out.open("a") as fh:
        for variant in ("plain", "truncated"):
            states, meta = [], []
            for s, (r_true, b_true) in labelled:
                cid = f"{s.file}:{s.line}:{s.finding.symbol}"
                if (cid, variant) in done:
                    continue
                st = evidence.render(s, name, a.old, a.new)
                if variant == "truncated":
                    st["consumer"]["code"] = truncated(s)
                states.append(st); meta.append((cid, r_true, b_true, s.finding.message))
            for (cid, r_true, b_true, msg), v in zip(meta, verify.verify(states)):
                fh.write(json.dumps({"id": cid, "variant": variant, "message": msg, "reach_true": r_true, "break_true": b_true,
                                     "reach": v.reach, "break": v.break_choice, "conf": v.break_confidence,
                                     "probs": v.break_probabilities, "tokens": v.input_tokens}) + "\n")
    report(out)


def report(path):
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    for variant in ("plain", "truncated"):
        rs = [r for r in rows if r["variant"] == variant]
        if not rs:
            continue
        print(f"\n== {variant}: n={len(rs)} ==")
        print("reach reliability (bin: n, hit rate = fraction truly reached):")
        for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
            b = [r for r in rs if lo <= r["reach"] < lo + 0.2 or (lo == 0.8 and r["reach"] == 1.0)]
            if b:
                print(f"   [{lo:.1f},{lo+0.2:.1f}) n={len(b):3d}  truly-reached={sum(r['reach_true'] for r in b)/len(b):.2f}")
        reached = [r for r in rs if r["reach_true"]]
        print("break reliability among truly-reached spans (confidence bin: n, accuracy):")
        for lo in (0.0, 0.5, 0.8, 0.9):
            hi = {0.0: 0.5, 0.5: 0.8, 0.8: 0.9, 0.9: 1.01}[lo]
            b = [r for r in reached if lo <= r["conf"] < hi]
            if b:
                acc = sum((r["break"] == "breaks_consumer") == r["break_true"] for r in b) / len(b)
                print(f"   conf [{lo:.1f},{hi if hi < 1.01 else 1.0:.1f}) n={len(b):3d}  accuracy={acc:.2f}")
        # threshold search: highest recall subject to precision >= 0.95 (YES) and miss rate <= 0.05 (NO)
        best_yes = best_no = None
        for t in (x / 20 for x in range(10, 20)):
            sel = [r for r in rs if r["reach"] >= t]
            if sel and sum(r["reach_true"] for r in sel) / len(sel) >= 0.95:
                best_yes = t; break
        for t in (x / 20 for x in range(10, 0, -1)):
            sel = [r for r in rs if r["reach"] < t]
            if sel and sum(r["reach_true"] for r in sel) / len(sel) <= 0.05:
                best_no = t; break
        print(f"thresholds from data: REACH_YES={best_yes}  REACH_NO={best_no}")


if __name__ == "__main__":
    if sys.argv[1:] == ["report"]:
        for p in ROOT.glob("calibration-*.jsonl"):
            print(f"##### {p.name}"); report(p)
    else:
        main()
