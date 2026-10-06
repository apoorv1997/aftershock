#!/usr/bin/env python3
"""Hand-written hard consumer-reach cases: the callee is hidden behind indirection.
Writes spike/cases/hard.jsonl. Labels are by construction."""
import json, pathlib

CASES = [
    ("kwargs-dict", True, 'kwargs = {"app": app, "base_url": "http://testserver"}\nclient = httpx.AsyncClient(**kwargs)'),
    ("kwargs-dict-merge", True, 'app_kwargs = dict(app=app)\nclient = httpx.AsyncClient(base_url="http://t", **app_kwargs)'),
    ("transport-variable", False, 'transport = httpx.ASGITransport(app=app)\nclient = httpx.AsyncClient(transport=transport, base_url="http://t")'),
    ("bare-import", True, 'from httpx import AsyncClient\n\nclient = AsyncClient(app=app)'),
    ("subclass", True, 'class TestClient(httpx.AsyncClient):\n    pass\n\nclient = TestClient(app=app)'),
    ("starlette-testclient", False, 'from starlette.testclient import TestClient\n\nclient = TestClient(app=app)'),
    ("variable-named-proxies-as-mounts", False, 'proxies = {"http://": httpx.HTTPTransport(proxy="http://proxy")}\nclient = httpx.Client(mounts=proxies)'),
    ("wrapper-function", True, 'def make_client(**kw):\n    return httpx.Client(**kw)\n\nclient = make_client(proxies="http://proxy")'),
    ("new-singular-proxy", False, 'client = httpx.Client(proxy="http://proxy")'),
    ("removed-param-with-none", True, 'resp = httpx.get(url, proxies=None)'),
    ("requests-session", False, 'session = requests.Session()\nresp = session.get(url, proxies=p)'),
    ("alias-module", True, 'import httpx as hx\n\nclient = hx.Client(proxies={"all://": "http://proxy"})'),
    ("functools-partial", True, 'from functools import partial\nmake = partial(httpx.AsyncClient, app=app)\nclient = make(base_url="http://t")'),
    ("commented-out", False, '# client = httpx.AsyncClient(app=app)  # old way\nclient = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))'),
]

out = pathlib.Path(__file__).with_suffix(".jsonl")
with out.open("w") as f:
    for name, label, code in CASES:
        kw = "app" if "app" in code else "proxies"
        f.write(json.dumps(dict(id=f"hard:{name}", ref="hand", path=f"hard/{name}.py", line=1, keyword=kw,
                                callee="?", label=label, code=code, context=code, source="hand")) + "\n")
print(f"{len(CASES)} hard cases -> {out}  positives={sum(l for _, l, _ in CASES)}")
