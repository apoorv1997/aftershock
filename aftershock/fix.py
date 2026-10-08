"""Proposed fixes for verified consequences, most certain first. Every fix is a list of fixcheck.Edit plus how it was
derived; whether it holds up is fixcheck's job, never this module's.

  keyword   a moved / now keyword-only parameter: pass the same arguments by name. Behaviour-identical by
            construction when every name exists in the new signature; refused otherwise (a renamed parameter).
  moved     a removed object whose exact name is defined in one other module at the new version: import it from
            there. `identical` when its definition is unchanged — otherwise the object may behave differently.
  analogy   the edit the change itself made to its own callers (`from prefect.cli.root import app` ->
            `from prefect.cli._app import app`, eleven times), applied to the line it missed. Only as good as the
            pattern: Prefect's script also needed the dropped `typer`, which fixcheck reports."""
from __future__ import annotations

import ast
import collections
import difflib
import re
import subprocess
from dataclasses import dataclass, field

from . import contracts, positional, reach
from .fixcheck import Edit


@dataclass
class Fix:
    edits: list[Edit]
    method: str                  # keyword / moved / analogy
    note: str = ""
    exact: bool = False          # behaviour-identical by construction (keyword) or an unchanged definition (moved)
    extra: dict = field(default_factory=dict)


def _git(lib_root, *a) -> str:
    return subprocess.run(["git", "-C", lib_root, *a], capture_output=True, text=True).stdout


def _fn(lib_root: str, ref: str, path: str, object_name: str):
    """The implementation's FunctionDef of `object_name` at `ref` (overload stubs skipped), or None."""
    return positional._def_at(lib_root, ref, path, object_name)


def _call_at(src: str, line: int, names: set[str]):
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and n.lineno <= line <= n.end_lineno and reach._callee(n) in names:
            return n
    return None


def _replace_node(src: str, node, text: str) -> Edit:
    """An Edit over the node's whole lines, with the node's own columns replaced by `text`."""
    lines = src.splitlines()
    first, last = lines[node.lineno - 1], lines[node.end_lineno - 1]
    new = first[:node.col_offset] + text + last[node.end_col_offset:]
    return Edit("", node.lineno, node.end_lineno, new)


def keyword(span, src: str, lib_root: str, old_ref: str, new_ref: str) -> Fix | None:
    f = span.finding
    m = f.message
    if not ("Positional parameter was moved" in m or ("Parameter kind was changed" in m and "keyword-only" in m)):
        return None
    obj = f.object_name
    name = obj.split(".")[-1] if obj.split(".")[-1] != "__init__" else obj.split(".")[-2]
    call = _call_at(src, span.line, {name, span.alias} - {""})
    old_pos = positional.positional_params(lib_root, old_ref, f.path, obj)
    new_fn = _fn(lib_root, new_ref, f.path, obj)
    if call is None or old_pos is None or new_fn is None or any(isinstance(a, ast.Starred) for a in call.args):
        return None
    kw_ok = {a.arg for a in new_fn.args.args + new_fn.args.kwonlyargs} | ({"*"} if new_fn.args.kwarg else set())
    if len(call.args) > len(old_pos):
        return None
    names = old_pos[:len(call.args)]
    if any(n not in kw_ok and "*" not in kw_ok for n in names):
        return None                                   # renamed or positional-only now: not a mechanical fix
    seg = lambda n: ast.get_source_segment(src, n)
    args = [f"{n}={seg(a)}" for n, a in zip(names, call.args)] + [seg(k) if k.arg is None else f"{k.arg}={seg(k.value)}"
                                                                 for k in call.keywords]
    e = _replace_node(src, call, f"{seg(call.func)}({', '.join(args)})")
    return Fix([Edit(span.file, e.start, e.end, e.text)], "keyword", f"pass {', '.join(names)} by name", exact=True)


def _homes(lib_root: str, package: str, search: str | None, ref: str, name: str, exclude: str) -> list[str]:
    """Modules of the library that DEFINE `name` (def / class / top-level assignment) at `ref`, `exclude` left out."""
    pkg_dir = f"{search + '/' if search else ''}{package}"
    # git grep -E is POSIX ERE: no \b or \s (handover gotcha)
    pat = rf"^(async def|def|class) {re.escape(name)}([^A-Za-z0-9_]|$)|^{re.escape(name)} *[:=]"
    out = []
    for path in _git(lib_root, "grep", "-lE", pat, ref, "--", pkg_dir).split():
        path = path.split(":", 1)[1]
        mod = reach.module_of(path[len(search) + 1:] if search else path)
        if mod != exclude:
            out.append(mod)
    return sorted(set(out))


def _definition(lib_root, ref, module, search, name):
    src = None
    base = f"{search}/" if search else ""
    for cand in (f"{base}{module.replace('.', '/')}.py", f"{base}{module.replace('.', '/')}/__init__.py"):
        r = subprocess.run(["git", "-C", lib_root, "show", f"{ref}:{cand}"], capture_output=True, text=True)
        if r.returncode == 0:
            src = r.stdout
            break
    if src is None:
        return None
    for n in ast.parse(src).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            return ast.dump(n)
    return None


def moved(span, src: str, lib_root: str, package: str, search: str | None, old_ref: str, new_ref: str) -> Fix | None:
    """Rewrite the import that binds a removed module-level object (or every name from a removed module) to the one
    module that defines the same name at the new version."""
    f = span.finding
    if "Public object was removed" not in f.message or ("." in f.object_name):
        return None
    tree = ast.parse(src)
    own = reach.module_of(span.file)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        mod = reach._abs_module(node, own, span.file.endswith("__init__.py"))
        if mod != f.module:
            continue
        wanted = [a for a in node.names if f.object_name == "<module>" or a.name == f.object_name]
        if not wanted:
            continue
        lines, notes, identical = [], [], True
        for a in wanted:
            homes = _homes(lib_root, package, search, new_ref, a.name, f.module)
            if len(homes) != 1:
                return None                       # none, or ambiguous: not mechanical
            same = _definition(lib_root, old_ref, f.module, search, a.name) == _definition(lib_root, new_ref, homes[0], search, a.name)
            identical &= same
            lines.append(f"from {homes[0]} import {a.name}" + (f" as {a.asname}" if a.asname else ""))
            notes.append(f"{a.name} -> {homes[0]}{'' if same else ' (definition changed)'}")
        rest = [a for a in node.names if a not in wanted]
        indent = src.splitlines()[node.lineno - 1][:node.col_offset]
        text = "\n".join(indent + l for l in lines)
        if rest:
            text = indent + f"from {'.' * node.level}{node.module or ''} import " + \
                   ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in rest) + "\n" + text
        return Fix([Edit(span.file, node.lineno, node.end_lineno, text)], "moved", "; ".join(notes), exact=identical)
    return None


def substitutions(diff: str, needle: str) -> collections.Counter:
    """(old, new) substring edits the diff made on lines that mention `needle`: each removed line is paired with the
    most similar added line of its hunk, and the differing middle (common prefix and suffix stripped, widened to whole
    tokens) is the edit."""
    out = collections.Counter()
    for hunk in re.split(r"^@@.*$", diff, flags=re.M):
        rem = [l[1:] for l in hunk.splitlines() if l.startswith("-") and not l.startswith("---") and needle in l]
        add = [l[1:] for l in hunk.splitlines() if l.startswith("+") and not l.startswith("+++")]
        for r in rem:
            best = max(add, key=lambda a: difflib.SequenceMatcher(None, r, a).ratio(), default=None)
            if best is None or difflib.SequenceMatcher(None, r, best).ratio() < 0.5:
                continue
            i = 0
            while i < min(len(r), len(best)) and r[i] == best[i]:
                i += 1
            j = 0
            while j < min(len(r), len(best)) - i and r[-1 - j] == best[-1 - j]:
                j += 1
            while i > 0 and (r[i - 1].isalnum() or r[i - 1] in "._"):       # widen to whole dotted tokens
                i -= 1
            while j > 0 and (r[len(r) - j].isalnum() or r[len(r) - j] in "._"):
                j -= 1
            old, new = r[i:len(r) - j], best[i:len(best) - j]
            balanced = all(new.count(o) - new.count(c) == old.count(o) - old.count(c) for o, c in ("()", "[]", "{}"))
            if old.strip() and old != new and needle in old and balanced:     # never half of a multi-line edit
                out[(old, new)] += 1
    return out


def analogy(span, src: str, diff: str) -> Fix | None:
    """Apply the most frequent substitution the change made on lines naming the removed object or module."""
    f = span.finding
    needle = f.module if f.object_name == "<module>" else f.object_name.split(".")[-1]
    subs = substitutions(diff, needle)
    lines = src.splitlines()
    from .fixcheck import _parse_fragment
    for (old, new), n in subs.most_common():
        for ln in range(span.line, (span.line + span.code.count("\n")) + 1):
            if old in lines[ln - 1] and _parse_fragment(lines[ln - 1].replace(old, new, 1)) is not None:
                return Fix([Edit(span.file, ln, ln, lines[ln - 1].replace(old, new, 1))], "analogy",
                           f"the change made `{old}` -> `{new}` {n} time(s) elsewhere", extra={"count": n})
    return None


def propose(span, src: str, lib_root: str, package: str, search: str | None, old_ref: str, new_ref: str,
            diff: str = "") -> list[Fix]:
    """Every fix a generator can make, most certain first."""
    out = []
    for gen in (lambda: keyword(span, src, lib_root, old_ref, new_ref),
                lambda: moved(span, src, lib_root, package, search, old_ref, new_ref),
                lambda: analogy(span, src, diff) if diff else None):
        try:
            fx = gen()
        except (SyntaxError, ValueError):
            fx = None
        if fx:
            out.append(fx)
    return out


def propose_file(spans, src: str, lib_root: str, package: str, search: str | None, old_ref: str, new_ref: str,
                 diff: str = "") -> list[Fix]:
    """One fix per method for ALL uses of a change in one file — a fixed line in a file that still imports the removed
    object elsewhere breaks at that import, so the unit is the file. Whether the union clears the file is fixcheck's
    call; a method is dropped only when it proposes two different edits for the same or overlapping lines."""
    per_method: dict[str, list[Fix]] = collections.defaultdict(list)
    for s in spans:
        for fx in propose(s, src, lib_root, package, search, old_ref, new_ref, diff):
            per_method[fx.method].append(fx)
    out = []
    for method, fixes in per_method.items():
        edits: dict[tuple[int, int], Edit] = {}
        clash = False
        for fx in fixes:
            for e in fx.edits:
                k = (e.start, e.end)
                clash |= k in edits and edits[k].text != e.text
                edits.setdefault(k, e)
        ranges = sorted(edits)
        if clash or any(b[0] <= a[1] for a, b in zip(ranges, ranges[1:])):
            continue
        out.append(Fix([edits[k] for k in ranges], method, "; ".join(dict.fromkeys(fx.note for fx in fixes)),
                       exact=all(fx.exact for fx in fixes), extra={"spans": len(spans), "proposed_for": len(fixes)}))
    return out
