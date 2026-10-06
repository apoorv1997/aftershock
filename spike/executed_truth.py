#!/usr/bin/env python3
"""Executed truth for verified consequences: overlay the changed package from commit C onto the repository at C^1
(every other package still written against the old API), run the consumers' own tests, and see which of cie's
verified lines actually raise. A flagged line that never raises is "not exercised", not "false": test coverage
decides that. Frames that raise on lines cie never flagged, naming a changed object, are missed consequences.

Built for a uv-workspace monorepo whose tests are export-ignored and whose shared libraries are vendored from a
second repository (`--utils`, `--vendor-libs`), as of the commit's date.
Usage: executed_truth.py --repo <service> --utils <shared-libs> --vendor-libs log,auth --rows <history-bench.jsonl> --shas a,b,c --out results.jsonl"""
from __future__ import annotations

import argparse, collections, json, os, pathlib, re, shutil, subprocess, tempfile, time


def git(repo, *a, **kw):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, **kw).stdout


def export(repo, ref, dest):
    subprocess.run(f"git -C {repo} archive {ref} | tar -x -C {dest}", shell=True, check=True)
    missing = 0
    for f in git(repo, "ls-tree", "-r", "--name-only", ref).splitlines():     # export-ignored tests/, scripts/, eval/, ...
        p = pathlib.Path(dest) / f
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(subprocess.run(["git", "-C", repo, "show", f"{ref}:{f}"], capture_output=True).stdout); missing += 1
    return missing


def vendor(utils, ref, dest, libs):
    ext = pathlib.Path(dest) / "libs" / "_external"; ext.mkdir(parents=True, exist_ok=True)
    for lib in libs:
        d = ext / lib; d.mkdir(exist_ok=True)
        subprocess.run(f"git -C {utils} archive {ref} python/{lib} | tar -x -C {d} --strip-components=2", shell=True, check=False)


FRAME = re.compile(r"^(?:INTERNALERROR> )?(?:\s*File \"(?P<f1>[^\"]+)\", line (?P<l1>\d+)|(?P<f2>[^\s:]+\.py):(?P<l2>\d+): )", re.M)
COLLECT = re.compile(r"ERROR collecting (\S+)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True); ap.add_argument("--utils", help="repository the vendored libraries come from"); ap.add_argument("--rows", required=True)
    ap.add_argument("--shas", required=True); ap.add_argument("--out", required=True); ap.add_argument("--pytest-timeout", type=int, default=1500)
    ap.add_argument("--vendor-libs", default="", help="comma-separated directories under python/ in --utils to vendor")
    ap.add_argument("--utils-branch", default="origin/main", help="branch of --utils to take the libraries from")
    ap.add_argument("--keep-output", help="directory to save each commit's raw pytest output")
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.rows)]
    out = open(a.out, "a")
    for short in a.shas.split(","):
        rs = [r for r in rows if r["sha"].startswith(short) and r["status"] in ("breaks_consumer", "needs_review")]
        if not rs:
            print(f"{short}: no verified/review rows"); continue
        sha = rs[0]["sha"]; date = rs[0]["date"]
        pkgs = sorted({r["finding"].split(".")[0] for r in rs})
        tree_new = git(a.repo, "ls-tree", "-r", "--name-only", sha).splitlines()
        tree_old = git(a.repo, "ls-tree", "-r", "--name-only", sha + "^1").splitlines()
        # overlay only the files that DEFINE the changed objects (finding module -> path); every caller, in any
        # package, stays at C^1 and is therefore written against the old API
        def path_of(module, tree):
            rel = module.replace(".", "/")
            for f in tree:
                if f.endswith(f"/{rel}.py") or f == f"{rel}.py" or f.endswith(f"/{rel}/__init__.py"):
                    return f
            return None
        mods = {r["finding"].split(":")[0].rsplit(".", 1)[0] if not r["finding"].startswith("unsurfaced") else None for r in rs} - {None}
        mods = {m[:-len(".<module>")] if m.endswith(".<module>") else m for m in mods}
        mods = {m.rsplit(".", 1)[0] if re.search(r"\.[A-Z]\w*$", m) and path_of(m, tree_old) is None else m for m in mods}   # Class.member -> module
        overlay_files = sorted({p for m in mods for p in [path_of(m, tree_old), path_of(m, tree_new)] if p})
        dirs = overlay_files
        t0 = time.time(); tmp = tempfile.mkdtemp(prefix="cie-exec-")
        try:
            restored = export(a.repo, sha + "^1", tmp)
            if a.utils and a.vendor_libs:
                uref = git(a.utils, "rev-list", "-1", f"--before={date}T23:59:59", a.utils_branch).strip() or a.utils_branch
                vendor(a.utils, uref, tmp, [l for l in a.vendor_libs.split(",") if l])
            for f in dirs:                                              # definition files at C (deleted at C -> removed), callers at C^1
                pathlib.Path(tmp, f).unlink(missing_ok=True)
                if f in tree_new:
                    subprocess.run(f"git -C {a.repo} archive {sha} {f} | tar -x -C {tmp}", shell=True, check=False)
            sync = subprocess.run(["uvx", "--from", "uv==0.12.10", "uv", "sync", "--all-packages", "--all-extras", "--frozen"], cwd=tmp, capture_output=True, text=True)
            if sync.returncode:
                sync = subprocess.run(["uvx", "--from", "uv==0.12.10", "uv", "sync", "--all-packages", "--all-extras"], cwd=tmp, capture_output=True, text=True)
            if sync.returncode:
                rec = {"sha": sha, "date": date, "packages": pkgs, "status": "env_failed", "detail": sync.stderr.strip()[-300:]}
                print(f"{short} {date} ENV FAILED: {rec['detail'][-160:]}"); out.write(json.dumps(rec) + "\n"); out.flush(); continue
            fix = os.path.join(tmp, "scripts", "fix-macos-editable-pth.sh")
            if os.path.exists(fix):
                subprocess.run(["bash", fix], cwd=tmp, capture_output=True)
            test_dirs = [str(p.relative_to(tmp)) for p in sorted(pathlib.Path(tmp).glob("*/*/tests"))]
            text = ""
            for td in test_dirs:                                        # one pytest per package: shared conftests clash otherwise
                try:
                    pt = subprocess.run([os.path.join(tmp, ".venv/bin/python"), "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", "--maxfail=1000", td],
                                        cwd=tmp, capture_output=True, text=True, timeout=a.pytest_timeout)
                    text += f"\n##### {td}\n" + pt.stdout + pt.stderr
                except subprocess.TimeoutExpired as e:
                    text += f"\n##### {td} TIMEOUT\n" + ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
            if a.keep_output:
                pathlib.Path(a.keep_output).mkdir(parents=True, exist_ok=True)
                pathlib.Path(a.keep_output, f"{short}.txt").write_text(text)
            frames = set(); real_tmp = os.path.realpath(tmp)
            for m in FRAME.finditer(text):
                f = m.group("f1") or m.group("f2"); ln = int(m.group("l1") or m.group("l2"))
                if os.path.isabs(f):
                    rf = os.path.realpath(f)
                    f = os.path.relpath(rf, real_tmp) if rf.startswith(real_tmp) else f
                if f.startswith((".venv", "libs/_external")) or "/site-packages/" in f:
                    continue
                frames.add((f, ln))
            collect_err = {os.path.relpath(os.path.realpath(m.group(1)), real_tmp) if os.path.isabs(m.group(1)) else m.group(1) for m in COLLECT.finditer(text)}
            files_raising = {f for f, _ in frames} | collect_err
            # a package whose __init__ (or any module on the import chain) raised cannot be imported at all:
            # every verified line under it is a realised consequence, whichever frame the traceback stopped at
            broken_dirs = {os.path.dirname(f) for f in files_raising if f.endswith("__init__.py")}
            broken_dirs |= {os.path.dirname(f) for f in files_raising if not re.search(r"(^|/)tests?/", f)}
            tots = collections.Counter()
            for m in re.finditer(r"(\d+) (failed|passed|errors?|skipped)\b", text):
                tots[m.group(2).rstrip("s")] += int(m.group(1))
            summary = re.match(r"(.*)", ", ".join(f"{v} {k}" for k, v in sorted(tots.items()))) if tots else None
            internal = "INTERNALERROR" in text and not tots
            ver = [r for r in rs if r["status"] == "breaks_consumer" and r["file"] not in dirs]      # same-file callers cannot be separated
            rev = [r for r in rs if r["status"] == "needs_review" and r["file"] not in dirs]
            raised = [r for r in ver if (r["file"], r["line"]) in frames]
            raised_file = [r for r in ver if r not in raised and (r["file"] in files_raising or any(r["file"].startswith(d + "/") for d in broken_dirs))]
            unexercised = [r for r in ver if r not in raised and r not in raised_file]
            names = {r["finding"].split(":")[0].split(".")[-1] for r in rs}
            flagged = {(r["file"], r["line"]) for r in rs}
            missed = []
            for f, ln in sorted(frames):
                if (f, ln) in flagged or f in dirs or re.search(r"(^|/)tests?/", f):
                    continue
                try:
                    line = (pathlib.Path(tmp) / f).read_text(errors="replace").splitlines()[ln - 1]
                except (OSError, IndexError):
                    continue
                if any(re.search(rf"\b{re.escape(n)}\b", line) for n in names):
                    missed.append((f, ln, line.strip()[:100]))
            rec = {"sha": sha, "date": date, "packages": pkgs, "overlay": dirs, "restored_files": restored, "tests": summary.group(1) if summary else ("INTERNALERROR (collection crashed)" if internal else (text.strip().splitlines()[-1][:120] if text.strip() else "?")),
                   "verified_outside": len(ver), "raised_at_line": len(raised), "raised_in_file": len(raised_file), "unexercised": len(unexercised),
                   "review_outside": len(rev), "review_raised": sum(1 for r in rev if (r["file"], r["line"]) in frames or r["file"] in files_raising or any(r["file"].startswith(d + "/") for d in broken_dirs)),
                   "missed": missed[:20], "frames": len(frames), "seconds": round(time.time() - t0)}
            print(f"{short} {date} {','.join(pkgs)}: tests [{rec['tests']}] verified outside pkg {len(ver)} → raised at line {len(raised)}, file raises {len(raised_file)}, unexercised {len(unexercised)}; "
                  f"review {len(rev)} → raised {rec['review_raised']}; missed {len(missed)}; {rec['seconds']}s")
            for f, ln, line in missed[:5]:
                print(f"      MISSED {f}:{ln}  {line}")
            out.write(json.dumps(rec) + "\n"); out.flush()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
