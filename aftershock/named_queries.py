"""HTTP contract between a consumer and a data service's named-query registry.

The service dispatches `POST /query/{name}` with `{"params": {...}}` to `name(connector, **params)`.
An unknown key or a missing required parameter is a TypeError → 400. So the contract per query is its
parameter list, and both sides are readable from source:
  provider  @query async def q_orders(connector, a: str, b: str = ...) in <registry dir>/*.py
  consumer  query_url("q_orders") ... json={"params": {...}}   or   _fetch_named("q_orders", {...}, ...)
Everything here is deterministic. A payload that is not a dict literal is reported for review."""
from __future__ import annotations

import ast
import pathlib
import subprocess
from dataclasses import dataclass, field


@dataclass(frozen=True)
class QuerySig:
    name: str
    required: tuple[str, ...]
    optional: tuple[str, ...]

    @property
    def params(self) -> set[str]:
        return set(self.required) | set(self.optional)


def registry(lib_root: str, ref: str, src_dir: str) -> dict[str, QuerySig]:
    """All named-query functions at `ref` under `src_dir` (the registry directory)."""
    files = subprocess.run(["git", "-C", lib_root, "ls-tree", "-r", "--name-only", ref, src_dir],
                           capture_output=True, text=True).stdout.split()
    out: dict[str, QuerySig] = {}
    for path in files:
        if not path.endswith(".py"):
            continue
        src = subprocess.run(["git", "-C", lib_root, "show", f"{ref}:{path}"], capture_output=True, text=True).stdout
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # every named query, whatever its name: the dispatch convention is a first parameter `connector`
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.args.args \
               and node.args.args[0].arg == "connector" and not node.name.startswith("_"):
                args = node.args
                names = [a.arg for a in args.posonlyargs + args.args if a.arg != "connector"]
                n_defaults = len(args.defaults)
                required = tuple(names[: len(names) - n_defaults]) if n_defaults else tuple(names)
                optional = tuple(names[len(names) - n_defaults:]) if n_defaults else ()
                kw_req = tuple(a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is None)
                kw_opt = tuple(a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None)
                out[node.name] = QuerySig(node.name, required + kw_req, optional + kw_opt)
    return out


@dataclass(frozen=True)
class Change:
    name: str
    kind: str                 # removed | required_added | param_removed | signature_changed
    detail: str


def diff(old: dict[str, QuerySig], new: dict[str, QuerySig]) -> list[Change]:
    out = []
    for name, o in old.items():
        n = new.get(name)
        if n is None:
            out.append(Change(name, "removed", "query no longer exists")); continue
        for p in set(n.required) - o.params:
            out.append(Change(name, "required_added", f"new required parameter `{p}`"))
        for p in o.params - n.params:
            out.append(Change(name, "param_removed", f"parameter `{p}` removed (sending it is a TypeError → 400)"))
        for p in set(n.optional) - o.params:
            # not a break, but the query's behaviour for callers that omit it may have changed
            out.append(Change(name, "optional_added", f"new optional parameter `{p}` — behaviour when omitted may differ"))
    return out


@dataclass
class CallSite:
    file: str
    line: int
    name: str
    keys: set[str] | None      # None = payload not a dict literal
    code: str


def _dict_keys(node: ast.AST) -> set[str] | None:
    if isinstance(node, ast.Dict) and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys):
        return {k.value for k in node.keys}
    return None


def call_sites(consumer_root: str | pathlib.Path, names: set[str] | None = None, prefix: str = "q_") -> list[CallSite]:
    """Every call carrying a string literal that names a registry query (or any literal starting with
    `prefix` when `names` is not given), with the params dict it sends when that dict is literal."""
    root = pathlib.Path(consumer_root)
    def is_query(v: str) -> bool:
        return v in names if names is not None else v.startswith(prefix)
    out = []
    for p in root.rglob("*.py"):
        if any(part in {".git", ".venv", "node_modules", "__pycache__"} for part in p.parts):
            continue
        try:
            src = p.read_text(errors="replace"); tree = ast.parse(src)
        except (SyntaxError, ValueError):
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            hits = [a.value for a in ast.walk(node) if isinstance(a, ast.Constant)
                    and isinstance(a.value, str) and is_query(a.value)]
            if not hits:
                continue
            keys = None
            for kw in node.keywords:                       # json={"params": {...}}
                if kw.arg == "json" and isinstance(kw.value, ast.Dict):
                    for k, v in zip(kw.value.keys, kw.value.values):
                        if isinstance(k, ast.Constant) and k.value == "params":
                            keys = _dict_keys(v)
                if kw.arg == "params":
                    keys = _dict_keys(kw.value)
            if keys is None:                               # _fetch_named("q_orders", {...}, ...)
                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        keys = _dict_keys(arg); break
            # only the outermost call for a given literal: skip if a parent call already captured it
            end = getattr(node, "end_lineno", node.lineno)
            out.append(CallSite(str(p.relative_to(root)), node.lineno, hits[0], keys,
                                "\n".join(lines[node.lineno - 1:end])))
    # drop inner duplicates (same file+name where an enclosing call spans the same line)
    seen, uniq = set(), []
    for c in sorted(out, key=lambda c: (c.file, c.line, -len(c.code))):
        if (c.file, c.line, c.name) in seen:
            continue
        seen.add((c.file, c.line, c.name)); uniq.append(c)
    return uniq


@dataclass(frozen=True)
class Injection:
    key: str
    file: str
    line: int
    conditional: bool          # assigned inside an `if`: sent only sometimes


def injected_keys(consumer_root: str | pathlib.Path) -> dict[str, Injection]:
    """Keys a shared client wrapper adds to every payload before sending.

    Pattern (a client base class that adds tenant scoping): a function that builds `{"params": X}` or passes
    `json=`/`params=` and, in the same body, assigns `X["<key>"] = ...` or `X.setdefault("<key>", ...)`.
    Such keys count as sent by every call site, so a per-call "omits <key>" is not reported for them."""
    root = pathlib.Path(consumer_root)
    out: dict[str, Injection] = {}
    for p in root.rglob("*.py"):
        if any(part in {".git", ".venv", "node_modules", "__pycache__"} for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except (SyntaxError, ValueError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # the payload variable: X in `{"params": X}`, `json=X` or `params=X`
            payload_vars: set[str] = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Dict):
                    for k, v in zip(n.keys, n.values):
                        if isinstance(k, ast.Constant) and k.value == "params" and isinstance(v, ast.Name):
                            payload_vars.add(v.id)
                if isinstance(n, ast.keyword) and n.arg in ("json", "params") and isinstance(n.value, ast.Name):
                    payload_vars.add(n.value.id)
            if not payload_vars:
                continue
            ifs = {id(c) for i in ast.walk(fn) if isinstance(i, ast.If) for c in ast.walk(i)}
            for n in ast.walk(fn):
                key, target = None, None
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Subscript) \
                   and isinstance(n.targets[0].value, ast.Name) \
                   and isinstance(n.targets[0].slice, ast.Constant) and isinstance(n.targets[0].slice.value, str):
                    key, target = n.targets[0].slice.value, n.targets[0].value.id
                elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "setdefault" \
                     and isinstance(n.func.value, ast.Name) \
                     and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                    key, target = n.args[0].value, n.func.value.id
                # only assignments on the payload dict itself (`row["HOUR_ID"] = ...` on a result row is not an injection)
                if key and target in payload_vars and key not in out:
                    out[key] = Injection(key, str(p.relative_to(root)), n.lineno, id(n) in ifs)
    return out


@dataclass
class Verdict:
    site: CallSite
    status: str                # breaks_consumer | compatible | needs_review | not_reached
    reason: str


def verify(sites: list[CallSite], old: dict[str, QuerySig], new: dict[str, QuerySig],
           injected: dict[str, Injection] | None = None) -> list[Verdict]:
    out = []
    injected = injected or {}
    changed = {c.name for c in diff(old, new) if c.kind != "optional_added"}
    for s in sites:
        if s.name not in old and s.name not in new:
            out.append(Verdict(s, "not_reached", "not a registry query")); continue
        if s.name not in new:
            out.append(Verdict(s, "breaks_consumer", "query removed")); continue
        if s.name not in changed:
            out.append(Verdict(s, "compatible", "signature unchanged")); continue
        sig = new[s.name]
        if s.keys is None:
            out.append(Verdict(s, "needs_review", "payload is not a dict literal; signature changed")); continue
        missing = set(sig.required) - s.keys
        via = {k: injected[k] for k in missing if k in injected}
        missing -= set(via)
        unknown = s.keys - sig.params
        if via and not missing and not unknown:
            out.append(Verdict(s, "compatible", "sends " + ", ".join(
                f"`{k}` via wrapper {i.file}:{i.line}{' (conditional)' if i.conditional else ''}" for k, i in via.items()))); continue
        if missing or unknown:
            out.append(Verdict(s, "breaks_consumer",
                               (f"does not send required {sorted(missing)}" if missing else "") +
                               (f" sends removed {sorted(unknown)}" if unknown else "")))
        else:
            out.append(Verdict(s, "compatible", "sends every required parameter, nothing removed"))
    return out
