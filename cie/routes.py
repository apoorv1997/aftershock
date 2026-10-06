"""HTTP route contract across languages: FastAPI/Starlette route paths a Python service exposes, old vs new,
against URL literals in a consumer written in anything (a TypeScript UI, another service).

Deterministic. A route is (method, path) from a decorator `@router.get("/path")` (any receiver name, any of
get/post/put/patch/delete/head/options/websocket/api_route/route), prefixed by `APIRouter(prefix=...)` in the
same file when there is one. A consumer reaches a path when a source line holds that path as a literal, with
`{param}` segments matched as one URL segment. Ceiling (ponytail): prefixes added by `include_router(prefix=)`
elsewhere are not applied; a path written from pieces (`base + "/x"`) is not seen."""
from __future__ import annotations

import ast, pathlib, re, subprocess
from dataclasses import dataclass

METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "websocket", "api_route", "route"}


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    function: str
    file: str
    line: int

    @property
    def key(self):
        return (self.method, self.path)


def _routes_in(src: str, file: str) -> list[Route]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    prefix = ""
    for n in ast.walk(tree):                      # APIRouter(prefix="/x") in this file
        if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "APIRouter":
            for kw in n.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    prefix = kw.value.value
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in METHODS):
                continue
            if not (d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str)):
                continue
            method = d.func.attr
            if method in ("api_route", "route"):
                for kw in d.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        method = ",".join(e.value.lower() for e in kw.value.elts if isinstance(e, ast.Constant))
            out.append(Route(method, prefix + d.args[0].value, n.name, file, d.lineno))
    return out


def _includes_in(src: str, file: str) -> list[tuple[str, str]]:
    """(included module, prefix) for every `x.include_router(<router>, prefix="/p")` in this file; the router
    argument `mod.router` / `alias.router` / a name imported `from .routes.x import router as r` resolves
    through the file's imports to the module that defines it."""
    from .reach import _import_map, module_of
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    imports = _import_map(tree, file)
    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "include_router" and n.args):
            continue
        prefix = next((kw.value.value for kw in n.keywords if kw.arg == "prefix" and isinstance(kw.value, ast.Constant)), "")
        a = n.args[0]
        if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name):       # mod.router
            mod, attr = imports.get(a.value.id, a.value.id), a.attr
        elif isinstance(a, ast.Name):                                            # router (imported under some name)
            q = imports.get(a.id, "")
            mod, attr = (q.rsplit(".", 1) if "." in q else ("", a.id))
        else:
            continue
        if mod:
            out.append((mod, attr, prefix, module_of(file)))
    return out


def _factory_aliases(src: str, file: str) -> dict[tuple[str, str], str]:
    """(module, name) -> module of the factory that built it: `router = create_speech_router(...)` with
    `from speechkit.fastapi import create_speech_router` means the routes live in speechkit.fastapi."""
    from .reach import _import_map, module_of
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    imports = _import_map(tree, file); mine = module_of(file); out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            f = n.value.func
            q = imports.get(f.id, "") if isinstance(f, ast.Name) else (imports.get(f.value.id, "") + "." + f.attr if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else "")
            if "." in q:
                out[(mine, n.targets[0].id)] = q.rsplit(".", 1)[0]
    return out


def collect(lib_root: str, ref: str, search: str = "", package: str = "") -> tuple[list[Route], dict]:
    """Raw decorator routes and include_router mounts under `search/package` (or the whole tree) at `ref`."""
    sub = f"{search + '/' if search else ''}{package}" if package else search
    files = subprocess.run(["git", "-C", lib_root, "ls-tree", "-r", "--name-only", ref, "--", sub or "."],
                           capture_output=True, text=True).stdout.splitlines()
    out, includes, aliases = [], [], {}
    for f in files:
        if not f.endswith(".py") or re.search(r"(^|/)tests?/", f):
            continue
        src = subprocess.run(["git", "-C", lib_root, "show", f"{ref}:{f}"], capture_output=True, text=True).stdout
        if "@" in src and any(m in src for m in METHODS):
            out.extend(_routes_in(src, f))
        if "include_router" in src:
            includes.extend(_includes_in(src, f))
        if "= " in src and "import" in src:
            aliases.update(_factory_aliases(src, f))
    return out, {"includes": includes, "aliases": aliases}


def estate_routes(specs: list[tuple[str, str]]) -> list[Route]:
    """Routes of several repositories with mounts resolved across them: a library router defined in one
    repository (a shared speech router) mounted with a prefix by a service in another (a chat service)."""
    out, mounts = [], {"includes": [], "aliases": {}}
    for lib_root, ref in specs:
        r, m = collect(lib_root, ref)
        out.extend(r); mounts["includes"].extend(m["includes"]); mounts["aliases"].update(m["aliases"])
    return apply_prefixes(out, mounts)


def routes(lib_root: str, ref: str, search: str = "", package: str = "") -> list[Route]:
    """All routes under `search/package` (or the whole tree) at `ref`, with include_router prefixes applied
    (two levels: an app including an api router that includes the route's router)."""
    out, includes = collect(lib_root, ref, search, package)
    return apply_prefixes(out, includes)


def apply_prefixes(out: list[Route], mounts: dict) -> list[Route]:
    from .reach import module_of
    def same(a: str, b: str) -> bool:
        """`routes.agents` (imported with the service dir on sys.path) is `api.routes.agents` (named from the repo root)."""
        return a == b or a.endswith("." + b) or b.endswith("." + a)

    aliases = mounts.get("aliases", {})
    includes: dict[str, list] = {}
    for mod, attr, prefix, parent in mounts.get("includes", []):
        includes.setdefault(mod, []).append((prefix, parent))
        for (amod, aname), target in aliases.items():                 # the mounted name was built by a factory elsewhere
            if aname == attr and same(amod, mod):
                includes.setdefault(target, []).append((prefix, parent))

    def full_prefixes(module: str, depth: int = 0) -> list[str]:
        """Every mount chain of a router module: a library router mounted by two services answers at two
        paths (`/api/speech/token` in one app, `/v1/speech/token` in another, 2026-09-30)."""
        if depth > 3:
            return [""]
        key = module if module in includes else next(iter(sorted((k for k in includes if "." in k and same(k, module)), key=len, reverse=True)), None)
        if key is None:                  # never a bare top-level name (`fastapi` matched `speechkit.fastapi`)
            return [""]
        out_ = []
        for prefix, parent in includes[key]:
            out_.extend(pp + prefix for pp in full_prefixes(parent, depth + 1))
        return sorted(set(out_)) or [""]
    return [Route(r.method, pre + r.path, r.function, r.file, r.line) for r in out for pre in full_prefixes(module_of(r.file))]


def diff(old: list[Route], new: list[Route], surviving: list[Route] | None = None) -> dict[str, list]:
    """Removed = an old (method, path) with no new route of that method whose path equals it or ends with it —
    a route that only moved under a mount prefix still answers a consumer that prepends its own base
    (`/code-audit/static/latest` became `/api/code-audit/static/latest`, 2026-09-30). `surviving` adds routes
    from other services of the estate: a route that moved to another repo is not gone either."""
    o = {r.key: r for r in old}; n = {r.key: r for r in new}
    alive = [r for r in new] + list(surviving or [])
    def survives(r: Route) -> bool:
        return any(a.method == r.method and (a.path == r.path or a.path.endswith(r.path)) for a in alive)
    return {"removed": [r for r in o.values() if not survives(r)], "added": [n[k] for k in n if k not in o]}


def _pattern(path: str) -> re.Pattern:
    """The path as a string literal (or the tail of one after a `${base}` piece), never inside a regex literal."""
    parts = re.split(r"\{[^}]*\}", path)
    return re.compile("(?<=['\"`}])" + "[^/'\"`?\\s]+".join(re.escape(p) for p in parts) + "(?![A-Za-z0-9_-])")


def matchable(path: str) -> bool:
    """A one-segment path (`/refresh`) is router-relative more often than not — its include_router prefix is
    unknown here (ceiling) — so it is not matched against consumers; the report still lists it as removed."""
    return path.count("/") >= 2


_MAP_ENTRY = re.compile(r"^\s*([A-Za-z_]\w*)\s*:\s*['\"`]")   # `authToken: "/auth/token",` inside an endpoint map


def _referenced_elsewhere(root: pathlib.Path, key: str, own: pathlib.Path, exts) -> bool:
    """Does `key` appear anywhere in the tree other than its own definition line? A map entry nothing names
    is a stale constant, not a call (a UI kept `authToken: "/auth/token"` after the route left, 2026-09-30)."""
    pat = re.compile(rf"\b{re.escape(key)}\b")
    for f in root.rglob("*"):
        if f.suffix not in exts or not f.is_file() or any(x in f.parts for x in ("node_modules", ".git", "dist", "build")):
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        n = len(pat.findall(text))
        if n > (1 if f == own else 0):
            return True
    return False


def consumers(consumer_root: str | pathlib.Path, paths: list[str], exts=(".ts", ".tsx", ".js", ".jsx", ".py", ".vue")) -> dict[str, list[tuple[str, int, str]]]:
    """path -> [(file, line, code)] for every source line in the consumer holding the path literal; a line that
    only defines a never-referenced map entry is reported with a `[stale constant]` note."""
    root = pathlib.Path(consumer_root)
    pats = {p: _pattern(p) for p in paths if matchable(p)}
    hits: dict[str, list] = {p: [] for p in paths}
    for f in root.rglob("*"):
        if f.suffix not in exts or not f.is_file() or any(x in f.parts for x in ("node_modules", ".git", "dist", "build", ".venv")):
            continue
        if re.search(r"(^|/)(tests?|__tests__)/|\.(test|spec)\.[jt]sx?$|(^|/)test_", str(f.relative_to(root))):
            continue                                        # production code only: tests mock these paths
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if "/" not in line or line.lstrip().startswith(("*", "//", "#", "/*")):
                continue                                    # comments are not calls
            for p, pat in pats.items():
                if pat.search(line):
                    m = _MAP_ENTRY.match(line)
                    note = "  [stale constant: never referenced]" if m and not _referenced_elsewhere(root, m.group(1), f, exts) else ""
                    hits[p].append((str(f.relative_to(root)), i, line.strip()[:140] + note))
    return hits
