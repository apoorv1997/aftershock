"""Deterministic routing: what code can decide, code decides; the model gets only the rest.

Shared by the CLI and the benchmarks so they can never drift apart."""
from __future__ import annotations

from . import positional
from .reach import Span
from .verify import SKIPPED, Verdict

RULE_BREAK = Verdict(1.0, "breaks_consumer", 1.0, {}, 0)
RULE_COMPAT = Verdict(1.0, "compatible", 1.0, {}, 0)
RULE_NOT_REACHED = Verdict(0.0, "compatible", 1.0, {}, 0)


def still_bound(s: Span, lib_root: str, new_ref: str) -> bool:
    """False when the 'removed' name is still bound somewhere in its module at the new ref — a conditional definition
    (`if TYPE_CHECKING: X = ...` / `else: class X`) griffe saw only one branch of. Such a name may or may not be
    importable at runtime, so it is a review item, not a verified break (pydantic IPvAnyAddress, 2026-10-01)."""
    import ast
    import subprocess
    f = s.finding
    if "." in f.object_name or f.object_name == "<module>":
        return True
    r = subprocess.run(["git", "-C", lib_root, "show", f"{new_ref}:{f.path}"], capture_output=True, text=True)
    if r.returncode:
        return True                                     # the file is gone: the removal is real
    try:
        tree = ast.parse(r.stdout)
    except SyntaxError:
        return True
    name = f.object_name
    for n in ast.walk(tree):
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return False
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store):
            return False
        if isinstance(n, (ast.Import, ast.ImportFrom)) and any((a.asname or a.name.split(".")[0]) == name for a in n.names):
            return False
        # still exported by name: `__all__`, or a lazy-export table (`_dynamic_imports = {"X": (parent, ".mod")}`)
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and any(isinstance(t, ast.Name) and t.id in ("__all__", "_dynamic_imports")
                                                              for t in (n.targets if isinstance(n, ast.Assign) else [n.target])):
            if n.value is not None and any(isinstance(c, ast.Constant) and c.value == name for c in ast.walk(n.value)):
                return False
    return True


def decide(s: Span, lib_root: str, old_ref: str, new_ref: str) -> Verdict | None:
    """A Verdict when a rule settles the span, None when the model must judge."""
    m, b = s.finding.message, None
    if "." in s.finding.object_name and s.ambiguous and s.receiver != "self":
        return None        # a member reached through an unknown receiver that the consumer also defines: the receiver's type decides
    if "Positional parameter was moved" in m:
        b = positional.breaks(s.code, s.finding, lib_root, old_ref, new_ref, s.alias)
    elif "Parameter was added as required" in m:
        b = positional.added_required_breaks(s.code, s.finding, s.receiver, s.alias, lib_root, new_ref)
    elif "Parameter kind was changed" in m:
        b = positional.kind_changed_breaks(s.code, s.finding, s.alias)
    elif "Parameter was removed" in m and s.resolved:
        b = positional.removed_param_breaks(s.code, s.finding, s.alias, lib_root, old_ref)
    elif "Public object was removed" in m and "." in s.finding.object_name and s.receiver == "self":
        b = True          # `self.<removed member>` inside the class that lost it
    elif s.finding.object_name == "<module>" and "Public object was removed" in m:
        b = True          # importing a deleted module fails at import time
    elif "Public object was removed" in m and s.resolved and not s.ambiguous:
        # the reference resolves through an import (or the module's own name) to exactly the removed
        # object: nothing is left for the model to judge (7 own-module uses sat at reach 0.2-0.4, 2026-09-29)
        if not still_bound(s, lib_root, new_ref):
            return SKIPPED     # still exported at the new ref: a person decides; the model cannot see the new module (pydantic, 2026-10-01)
        b = True
    if b == "not_reached":
        return RULE_NOT_REACHED
    if b is None:
        return None
    return RULE_BREAK if b else RULE_COMPAT
