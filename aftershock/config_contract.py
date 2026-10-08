"""Code <-> deployment config contract: which env keys code reads vs which the chart sets.

The cheapest cross-repo check there is, and the shape of two real incidents (a release predating its
chart wiring; env vars set for an exporter nobody reads). Deterministic; no model."""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field

_GETENV = re.compile(r"""os\.(?:getenv|environ\.get|environ\[)\(?\s*["']([A-Z][A-Z0-9_]+)["']""")
_SETTINGS_FIELD = re.compile(r"^\s{4}([A-Z][A-Z0-9_]+)\s*:", re.M)       # pydantic-style Settings class fields
_CHART_KEY = re.compile(r"^\s{2,}([A-Z][A-Z0-9_]+):", re.M)                # values.yaml env block keys
_TEMPLATE_NAME = re.compile(r"name:\s*([A-Z][A-Z0-9_]+)\s*$", re.M)        # templates env[].name


@dataclass
class ConfigContract:
    required_by_code: set[str] = None  # settings fields with no default: a missing key fails at boot

    read_by_code: set[str] = field(default_factory=set)
    set_by_chart: set[str] = field(default_factory=set)

    @property
    def set_but_unread(self) -> set[str]:
        return self.set_by_chart - self.read_by_code

    @property
    def read_but_unset(self) -> set[str]:
        return self.read_by_code - self.set_by_chart


class _NoEval(Exception):
    pass


def _nm(n) -> str:
    import ast
    return n.id if isinstance(n, ast.Name) else getattr(n, "attr", "")


def _eval(node, env: dict):
    """The tiny expression language an env_prefix helper is written in: literals, f-strings, names bound from the call,
    `x if c else y`, tuples, and .join/.upper/.lower. Anything else is not evaluated (never exec the scanned code)."""
    import ast
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in env:
        return env[node.id]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, env) for e in node.elts)
    if isinstance(node, ast.JoinedStr):
        return "".join(str(_eval(v.value if isinstance(v, ast.FormattedValue) else v, env)) for v in node.values)
    if isinstance(node, ast.IfExp):
        return _eval(node.body, env) if _eval(node.test, env) else _eval(node.orelse, env)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        obj = _eval(node.func.value, env)
        if node.func.attr in ("upper", "lower") and not node.args and isinstance(obj, str):
            return getattr(obj, node.func.attr)()
        if node.func.attr == "join" and len(node.args) == 1 and isinstance(obj, str):
            return obj.join(_eval(node.args[0], env))
    raise _NoEval


def collect_helpers(root) -> dict:
    """Functions that build a settings config from arguments (`build_settings_config(("server", "api"))` ->
    env_prefix `PREFECT_SERVER_API_`), by name, from every .py under root that mentions env_prefix."""
    import ast
    out = {}
    for p in pathlib.Path(root).rglob("*.py"):
        if any(part in {".git", ".venv", "node_modules"} for part in p.parts):
            continue
        try:
            src = p.read_text(errors="replace")
            if "env_prefix" not in src:
                continue
            tree = ast.parse(src)
        except (OSError, SyntaxError, ValueError):
            continue
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    isinstance(k, ast.keyword) and k.arg == "env_prefix" for k in ast.walk(fn)):
                out[fn.name] = fn
    return out


def _helper_prefix(call, helpers: dict):
    """env_prefix produced by calling a helper with literal arguments, or None."""
    import ast
    fn = helpers.get(_nm(call.func))
    if fn is None:
        return None
    a = fn.args
    params = [x.arg for x in a.args]
    env = {}
    defaults = [None] * (len(params) - len(a.defaults)) + list(a.defaults)
    for name, d in zip(params, defaults):
        if d is not None:
            try:
                env[name] = ast.literal_eval(d)
            except ValueError:
                if isinstance(d, ast.Call) and _nm(d.func) == "tuple" and not d.args:
                    env[name] = ()
    try:
        for name, v in zip(params, call.args):
            env[name] = ast.literal_eval(v)
        for kw in call.keywords:
            if kw.arg in params:
                env[kw.arg] = ast.literal_eval(kw.value)
        for st in fn.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                try:
                    env[st.targets[0].id] = _eval(st.value, env)
                except _NoEval:
                    pass
        for n in ast.walk(fn):
            if isinstance(n, ast.keyword) and n.arg == "env_prefix":
                return str(_eval(n.value, env))
    except (ValueError, _NoEval):
        return None
    return None


def _settings_class(cls, helpers: dict | None) -> tuple[bool, str]:
    """(is a pydantic settings class, env prefix). A subclass of any `...Settings` base counts (PrefectBaseSettings)."""
    import ast
    bases = {_nm(b) for b in cls.bases}
    is_settings = "BaseSettings" in bases or any(b.endswith("Settings") for b in bases)
    prefix = ""
    for n in ast.walk(cls):
        if isinstance(n, ast.Call):
            if _nm(n.func) == "SettingsConfigDict":
                for kw in n.keywords:
                    if kw.arg == "env_prefix" and isinstance(kw.value, ast.Constant):
                        prefix = str(kw.value.value)
            elif helpers and _nm(n.func) in helpers:
                hp = _helper_prefix(n, helpers)
                if hp is not None:
                    prefix = hp
        if isinstance(n, ast.ClassDef) and n.name == "Config":
            for st in n.body:
                if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "env_prefix" for t in st.targets) and isinstance(st.value, ast.Constant):
                    prefix = str(st.value.value)
    return is_settings, prefix


def _aliases(field_call) -> list[str]:
    """Env names a `Field(...)` pins: alias= / validation_alias= / env=, a plain string or `AliasChoices("a", "b", AliasPath(..))`."""
    import ast
    out = []
    for kw in field_call.keywords:
        if kw.arg not in ("alias", "validation_alias", "env"):
            continue
        v = kw.value
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            out.append(v.value)
        elif isinstance(v, ast.Call) and _nm(v.func) == "AliasChoices":
            out += [a.value for a in v.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    return out


def settings_keys(src: str, required: set[str] | None = None, helpers: dict | None = None) -> set[str]:
    """Env keys a pydantic Settings class reads: every field of a class deriving from BaseSettings, upper-cased
    behind the class's env_prefix (`model_config = SettingsConfigDict(env_prefix="APP_GW_")` or an inner
    `class Config: env_prefix = ...`); an explicit `Field(alias=...)` / `validation_alias=` wins.
    Also every UPPER_CASE annotated field of any class (the older hand-rolled style)."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    out = set()
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        is_settings, prefix = _settings_class(cls, helpers)
        for st in cls.body:
            if not (isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name)):
                continue
            name = st.target.id
            if name.startswith("_") or name == "model_config":
                continue
            aliases = _aliases(st.value) if isinstance(st.value, ast.Call) and _nm(st.value.func) == "Field" else []
            keys = [a.upper() for a in aliases] or [k for k in [((prefix + name).upper() if is_settings else (name if name.isupper() else None))] if k]
            has_default = st.value is not None and not (isinstance(st.value, ast.Call) and getattr(st.value.func, "id", getattr(st.value.func, "attr", "")) == "Field"
                                                        and not st.value.args and not any(kw.arg in ("default", "default_factory") for kw in st.value.keywords))
            for key in keys:
                out.add(key)
                if is_settings and not has_default and required is not None:
                    required.add(key)
    return out


def settings_defaults(src: str, helpers: dict | None = None) -> dict[str, str]:
    """Env key -> default expression source for every pydantic Settings field (and UPPER_CASE class fields).
    A new key, or a changed default, is a behaviour change for every deployment that does not set the key."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        is_settings, prefix = _settings_class(cls, helpers)
        for st in cls.body:
            if not (isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name)) or st.target.id.startswith("_") or st.target.id == "model_config":
                continue
            name = st.target.id; aliases = []; default = None
            v = st.value
            if isinstance(v, ast.Call) and _nm(v.func) == "Field":
                aliases = _aliases(v)
                for kw in v.keywords:
                    if kw.arg in ("default", "default_factory"):
                        default = ast.unparse(kw.value)
                if v.args:
                    default = ast.unparse(v.args[0])
            elif v is not None:
                default = ast.unparse(v)
            keys = [x.upper() for x in aliases] or [k for k in [((prefix + name).upper() if is_settings else (name if name.isupper() else None))] if k]
            for key in keys:
                out[key] = "(required)" if default is None else default[:60]
    return out


def scan(code_root: str | pathlib.Path, chart_paths: list[str | pathlib.Path], settings_file: str | None = None) -> ConfigContract:
    c = ConfigContract(); c.required_by_code = set()
    helpers = collect_helpers(code_root)
    for p in pathlib.Path(code_root).rglob("*.py"):
        if any(part in {".git", ".venv", "node_modules"} for part in p.parts) or not p.is_file():
            continue
        try:
            src = p.read_text(errors="replace")
        except OSError:
            continue
        c.read_by_code |= set(_GETENV.findall(src))
        if "BaseSettings" in src or "env_prefix" in src or re.search(r"(^|/)(config|settings)\w*\.py$", str(p)):
            c.read_by_code |= settings_keys(src, c.required_by_code, helpers)   # pydantic settings anywhere in the tree, no --settings needed
    if settings_file:
        c.read_by_code |= set(_SETTINGS_FIELD.findall(pathlib.Path(settings_file).read_text(errors="replace")))
        c.read_by_code |= settings_keys(pathlib.Path(settings_file).read_text(errors="replace"), c.required_by_code)
    for cp in chart_paths:
        cp = pathlib.Path(cp)
        files = list(cp.rglob("*.y*ml")) if cp.is_dir() else [cp]
        for f in files:
            if not f.is_file():
                continue
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            c.set_by_chart |= set(_CHART_KEY.findall(text)) | set(_TEMPLATE_NAME.findall(text))
    return c
