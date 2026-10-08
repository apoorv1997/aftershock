"""Does a proposed fix hold up? A ladder of checks, each reported; a fix is only as trusted as the highest rung it passed.

  parses    the patched file is valid Python
  resolves  every library import and library reference on the edited lines names a module and a binding that exist
            at the new ref
  clears    aftershock on the patched file (rules only): no break of the fixed change is left in it, and no new one appears
  runs      the patched module imports with the library at the new ref

A fix that deletes an argument or a call is flagged as a behaviour change whatever it passes: it silences the error by
dropping what the code asked for. And "runs" means the import succeeded, not that the code does what it did before:
Prefect #20838's escape (`from prefect.cli.root import app`) is import-fixed by `from prefect.cli._app import app`,
yet the script still drove the old typer API — its real fix, 145 days later, rewrote it."""
from __future__ import annotations

import ast
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from . import contracts, reach, rules

LADDER = ("parses", "resolves", "clears", "runs")


@dataclass(frozen=True)
class Edit:
    file: str        # consumer-relative path
    start: int       # 1-based, inclusive
    end: int         # 1-based, inclusive; end = start - 1 inserts before `start`
    text: str        # replacement lines ('' deletes them)


@dataclass
class Rung:
    name: str
    status: str      # pass / fail / not_run
    detail: str = ""


@dataclass
class FixCheck:
    rungs: list[Rung] = field(default_factory=list)
    behaviour_change: str = ""      # non-empty: what the fix dropped
    private: str = ""               # non-empty: private names of the library a consumer outside it would now import

    @property
    def level(self) -> str:
        """The highest rung passed with every rung below it passed too; 'fails' if the first one failed."""
        best = "fails"
        for r in self.rungs:
            if r.status != "pass":
                break
            best = r.name
        return best

    def summary(self) -> str:
        parts = [f"{r.name} {'✓' if r.status == 'pass' else ('✗' if r.status == 'fail' else '–')}"
                 + (f" ({r.detail})" if r.detail and r.status != "pass" else "") for r in self.rungs]
        return "; ".join(parts) + (f"; BEHAVIOUR CHANGE: {self.behaviour_change}" if self.behaviour_change else "") \
            + (f"; PRIVATE API: {self.private}" if self.private else "")


def apply(src: str, edits: list[Edit]) -> tuple[str, list[tuple[int, int]]]:
    """Patched source and the 1-based line ranges the edits occupy in it."""
    lines = src.splitlines()
    for e in sorted(edits, key=lambda e: e.start, reverse=True):     # bottom-up keeps earlier line numbers valid
        lines[e.start - 1:e.end] = e.text.splitlines() if e.text else []
    out, delta = [], 0                                                  # a range moves by the edits above it
    for e in sorted(edits, key=lambda e: e.start):
        n = len(e.text.splitlines()) if e.text else 0
        out.append((e.start + delta, e.start + delta + n - 1))
        delta += n - (e.end - e.start + 1)
    return "\n".join(lines) + "\n", out


def behaviour_change(old_src: str, new_src: str, edits: list[Edit]) -> str:
    """What an edit drops: a keyword argument, a positional argument, a call, or the whole statement."""
    old_lines, dropped = old_src.splitlines(), []
    for e in edits:
        before = "\n".join(old_lines[e.start - 1:e.end])
        if before.strip() and not e.text.strip():
            dropped.append(f"deletes `{before.strip()[:60]}`")
            continue
        b, a = _calls(before), _calls(e.text)
        if len(a) < len(b):
            dropped.append(f"removes {len(b) - len(a)} call(s)")
        for name, (npos, kws) in b.items():
            if name in a:
                apos, akws = a[name]
                if kws - akws - {None}:
                    dropped.append(f"drops `{', '.join(sorted(k for k in kws - akws if k))}=` from `{name}(...)`")
                if apos + len(akws) < npos + len(kws):
                    dropped.append(f"passes fewer arguments to `{name}(...)`")
    return "; ".join(dict.fromkeys(dropped))


def _calls(text: str) -> dict[str, tuple[int, set]]:
    tree = _parse_fragment(text)
    out = {}
    for n in ast.walk(tree) if tree else []:
        if isinstance(n, ast.Call):
            out[reach._callee(n)] = (len(n.args), {k.arg for k in n.keywords})
    return out


def _parse_fragment(text: str):
    import textwrap
    for t in (textwrap.dedent(text), textwrap.dedent(text) + "\n    pass"):
        try:
            return ast.parse(t)
        except SyntaxError:
            continue
    return None


def _module_file(lib_root: str, ref: str, module: str, search: str | None) -> str | None:
    base = f"{search}/" if search else ""
    for cand in (f"{base}{module.replace('.', '/')}.py", f"{base}{module.replace('.', '/')}/__init__.py"):
        r = subprocess.run(["git", "-C", lib_root, "show", f"{ref}:{cand}"], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
    return None


def declared_dependencies(lib_root: str, ref: str) -> set[str] | None:
    """Distribution names the repository's pyproject declares at `ref` (normalised: lower case, '-' -> '_'), or None."""
    import re
    import tomllib
    r = subprocess.run(["git", "-C", lib_root, "show", f"{ref}:pyproject.toml"], capture_output=True, text=True)
    if r.returncode:
        return None
    try:
        deps = tomllib.loads(r.stdout).get("project", {}).get("dependencies", [])
    except tomllib.TOMLDecodeError:
        return None
    return {re.split(r"[<>=!~;\[ (]", d.strip())[0].lower().replace("-", "_") for d in deps if d.strip()}


def resolves(new_src: str, rel: str, ranges, lib_root: str, package: str, new_ref: str, search: str | None,
             old_ref: str | None = None, same_repo: bool = False) -> Rung:
    """Every library module/name the edited lines import or reference exists at the new ref. For a file of the library's
    own repository (it runs in that repository's environment), also: nothing it imports is a dependency the change
    stopped declaring — Prefect #20838 dropped `typer`, so its script could not be fixed by an import path alone."""
    tree = ast.parse(new_src)
    modules = contracts.library_modules(lib_root, package, new_ref, search)
    imports = reach._import_map(tree, rel)
    own = reach.module_of(rel)
    on_edit = lambda n: any(a <= getattr(n, "lineno", -1) <= b for a, b in ranges)
    targets = set()
    for n in ast.walk(tree):
        if not on_edit(n):
            continue
        if isinstance(n, ast.Import):
            targets |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mod = reach._abs_module(n, own, rel.endswith("__init__.py"))
            targets |= {f"{mod}.{a.name}" for a in n.names if a.name != "*"}
        elif isinstance(n, (ast.Name, ast.Attribute)) and isinstance(getattr(n, "ctx", None), ast.Load):
            d = reach._dotted(n)
            if d and d.split(".")[0] in imports:
                targets.add(reach.resolve(d, imports, {}))
    missing = []
    for t in sorted(targets):
        if not (t == package or t.startswith(package + ".")):
            continue
        parts = t.split(".")
        k = max((i for i in range(1, len(parts) + 1) if ".".join(parts[:i]) in modules), default=0)
        if k == 0:
            missing.append(f"{t} (no such module)")
        elif k < len(parts):
            src = _module_file(lib_root, new_ref, ".".join(parts[:k]), search)
            if src is None or not contracts.binds(src, parts[k]):
                missing.append(f"{'.'.join(parts[:k])} has no `{parts[k]}`")
    if same_repo and old_ref:
        before, after = declared_dependencies(lib_root, old_ref), declared_dependencies(lib_root, new_ref)
        if before is not None and after is not None:
            dropped = before - after
            used = {a.name.split(".")[0].lower() for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                   {n.module.split(".")[0].lower() for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module}
            for d in sorted(used & dropped):        # module name taken as the distribution name: approximate (yaml/PyYAML)
                missing.append(f"imports `{d}`, which {package} no longer depends on")
    return Rung("resolves", "fail" if missing else "pass", "; ".join(missing))


def clears(old_src: str, new_src: str, rel: str, ranges, findings, target, lib_root: str, old_ref: str, new_ref: str,
           exports, modules) -> Rung:
    """No rule-verified break of the fixed change is left anywhere in the file — a fix that edits a use but keeps the
    import of a removed object still fails at that import — and no new break appears. Rules only (no model): what they
    leave undecided is counted, not judged."""
    def verified(src):
        with tempfile.TemporaryDirectory(prefix="aftershock-fix-") as d:
            p = pathlib.Path(d) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
            out, undecided = [], 0
            for s in reach.candidates(d, findings, exports=exports, modules=modules):
                v = rules.decide(s, lib_root, old_ref, new_ref)
                if v is None:
                    undecided += 1
                elif v.status == "breaks_consumer":
                    out.append(s)
            return out, undecided
    before, _ = verified(old_src)
    after, undecided = verified(new_src)
    targets = set(target) if isinstance(target, (list, tuple, set)) else {target}
    still = [s for s in after if s.finding in targets]          # anywhere in the file: the import line breaks first
    key = lambda s: (s.finding.symbol, s.code.strip())
    new = {key(s) for s in after} - {key(s) for s in before}
    if still:
        return Rung("clears", "fail", f"still breaks at line {still[0].line}")
    if new:
        return Rung("clears", "fail", f"introduces {len(new)} new break(s): {sorted(new)[0][1][:60]}")
    return Rung("clears", "pass", f"{undecided} span(s) left for the model" if undecided else "")


def runs(new_src: str, rel: str, consumer_root: str | None, lib_root: str, package: str, new_ref: str,
         search: str | None, python: str | None = None, timeout: int = 120) -> Rung:
    """Import the patched module with the library at the new ref. A missing third-party dependency is 'not_run',
    never a failure of the fix."""
    module = reach.module_of(rel)
    if not module:
        return Rung("runs", "not_run", "not an importable module")
    top = module.split(".")[0]
    with tempfile.TemporaryDirectory(prefix="aftershock-run-") as d:
        lib = pathlib.Path(d, "lib")
        lib.mkdir()
        pkg_dir = f"{search + '/' if search else ''}{package}"
        subprocess.run(f"git -C {lib_root} archive {new_ref} {pkg_dir} | tar -x -C {lib}", shell=True, check=False)
        path = [str(lib / search) if search else str(lib)]
        if top == package:                        # a module of the library itself: overlay it on the new tree
            target = lib / rel
        else:                                     # a consumer: copy its top-level package (or the lone script)
            cons = pathlib.Path(d, "cons")
            parts = rel.split("/")
            i = parts.index(top) if top in parts[:-1] else len(parts) - 1
            src_dir = pathlib.Path(consumer_root or ".", *parts[:i + 1])
            dst = cons.joinpath(*parts[:i + 1])
            if src_dir.is_dir():
                shutil.copytree(src_dir, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = cons / rel
            path.insert(0, str(cons.joinpath(*parts[:i])) if i else str(cons))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_src)
        env = {"PYTHONPATH": ":".join(path), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            exe = os.path.abspath(python) if python and "/" in python else (python or sys.executable)
            r = subprocess.run([exe, "-c", f"import {module}"], cwd=d, env=env,                 # cwd is the temp tree
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return Rung("runs", "not_run", f"import took over {timeout}s")
    if r.returncode == 0:
        return Rung("runs", "pass")
    last = (r.stderr.strip().splitlines() or ["?"])[-1]
    if last.startswith("ModuleNotFoundError"):
        missing = last.split("'")[1] if "'" in last else ""
        if missing and missing.split(".")[0] not in (package, top):
            return Rung("runs", "not_run", f"environment lacks `{missing.split('.')[0]}`")
    import re
    m = re.match(r"ImportError: cannot import name '([^']+)' from '([^']+)'", last)
    if m and m.group(2).split(".")[0] not in (package, top):     # a third-party version mismatch, not the fix
        return Rung("runs", "not_run", f"environment's `{m.group(2).split('.')[0]}` lacks `{m.group(1)}`")
    return Rung("runs", "fail", last[:160])


def check(old_src: str, rel: str, edits: list[Edit], target, findings, lib_root: str, package: str, old_ref: str,
          new_ref: str, search: str | None = None, consumer_root: str | None = None, python: str | None = None,
          run: bool = True) -> FixCheck:
    """Climb the ladder; stop at the first failed rung (a fix that does not parse cannot be resolved, and so on)."""
    new_src, ranges = apply(old_src, edits)
    fc = FixCheck(behaviour_change=behaviour_change(old_src, new_src, edits))
    if reach.module_of(rel).split(".")[0] != package:       # a consumer outside the library: its privates are not an API
        try:
            tree = ast.parse(new_src)
            own = reach.module_of(rel)
            fc.private = ", ".join(sorted({f"{reach._abs_module(n, own, rel.endswith('__init__.py'))}.{a.name}" for n in ast.walk(tree)
                                           if isinstance(n, ast.ImportFrom) and any(x <= n.lineno <= y for x, y in ranges)
                                           for a in n.names if a.name.startswith("_") and not a.name.startswith("__")
                                           and reach._abs_module(n, own, rel.endswith('__init__.py')).split(".")[0] == package}))
        except SyntaxError:
            pass
    try:
        ast.parse(new_src)
        fc.rungs.append(Rung("parses", "pass"))
    except SyntaxError as e:
        fc.rungs.append(Rung("parses", "fail", f"line {e.lineno}: {e.msg}"))
        return fc
    same_repo = subprocess.run(["git", "-C", lib_root, "cat-file", "-e", f"{old_ref}:{rel}"], capture_output=True).returncode == 0
    fc.rungs.append(resolves(new_src, rel, ranges, lib_root, package, new_ref, search, old_ref, same_repo))
    if fc.rungs[-1].status == "fail":
        return fc
    exports = contracts.package_exports(lib_root, package, old_ref, search)
    modules = contracts.library_modules(lib_root, package, old_ref, search)
    fc.rungs.append(clears(old_src, new_src, rel, ranges, findings, target, lib_root, old_ref, new_ref, exports, modules))
    if fc.rungs[-1].status == "fail":
        return fc
    fc.rungs.append(runs(new_src, rel, consumer_root, lib_root, package, new_ref, search, python) if run
                    else Rung("runs", "not_run", "not requested"))
    return fc
