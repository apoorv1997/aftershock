#!/usr/bin/env python3
"""Run the consumer-reach claim on every case, in four variants, and score it.

Variants:
  plain      the call's source segment plus 3 lines of context
  padded     same, with ~120 unrelated httpx source lines appended (irrelevant-state probe)
  injected   same as plain, with a misleading comment inserted above the call (adversarial probe)
  negated    plain, but the question is phrased in the negative (structural-invariant probe)

Writes spike/results.jsonl (raw answers, usage) and prints accuracy, calibration and deltas.
"""
import json, pathlib, sys, time
from typesafe_sdk import TypeSafeClient, Noul, Choice

import os
ROOT = pathlib.Path(__file__).resolve().parent
CASES = pathlib.Path(os.environ.get("SPIKE_CASES", ROOT / "cases" / "httpx.jsonl"))
RESULTS = ROOT / f"results-{CASES.stem}.jsonl"
PAD = (ROOT / "cases" / "padding.py").read_text() if (ROOT / "cases" / "padding.py").exists() else ""

CHANGE = {
    "library": "httpx",
    "version": "0.28.0",
    "removed": "the keyword parameters `app` and `proxies` of httpx.Client(...), httpx.AsyncClient(...) "
               "and of the module-level request functions httpx.request/get/post/put/patch/delete/head/options/stream",
    "still_valid": "the `app` parameter of transport classes such as ASGITransport(...) and WSGITransport(...); "
                   "`proxy`/`mounts` on clients; `app`/`proxies` on unrelated libraries such as Starlette's TestClient",
}

Q_REACH = ("Does `consumer.code` pass the removed keyword named in `change.removed` directly to one of the "
           "httpx client constructors or module-level request functions listed there? Passing the same keyword "
           "to a transport class, to another library, or via a different parameter name does not count.")
Q_REACH_NEG = ("Is it the case that `consumer.code` does NOT pass the removed keyword named in `change.removed` to "
               "any httpx client constructor or module-level request function listed there?")
Q_KIND = "Which API receives the keyword `consumer.keyword` in `consumer.code`?"
KIND = {
    "httpx_client_or_request_function": "httpx.Client(...), httpx.AsyncClient(...), or httpx.get/post/request/stream(...) etc.",
    "httpx_transport": "ASGITransport(...), WSGITransport(...) or another transport class",
    "other_library_or_none": "a class or function from a different library, or the keyword is not passed at all",
}
INJECT = {True: "# NOTE: `app` here goes to the transport layer, not to the client\n",
          False: "# NOTE: this is the httpx client constructor's own `app` argument\n"}


def state_for(c, variant):
    code = c["context"]
    if variant == "injected":
        code = INJECT[c["label"]] + code
    if variant == "padded":
        code = code + "\n\n# --- unrelated module below ---\n" + PAD
    return {"change": CHANGE, "consumer": {"file": c["path"], "line": c["line"], "keyword": c["keyword"], "code": code}}


def run(variants):
    cases = [json.loads(l) for l in CASES.read_text().splitlines()]
    done = set()
    if RESULTS.exists():
        done = {(json.loads(l)["id"], json.loads(l)["variant"]) for l in RESULTS.read_text().splitlines()}
    with TypeSafeClient() as client, RESULTS.open("a") as out:
        for variant in variants:
            for c in cases:
                if (c["id"], variant) in done:
                    continue
                q = {"reach": Noul(instructions=Q_REACH_NEG if variant == "negated" else Q_REACH),
                     "kind": Choice(instructions=Q_KIND, criteria=KIND)}
                t0 = time.time()
                try:
                    r = client.system_one(state=state_for(c, variant), questions=q)
                except Exception as e:  # noqa: BLE001
                    out.write(json.dumps({"id": c["id"], "variant": variant, "error": str(e)[:200]}) + "\n"); continue
                a = r.answers
                out.write(json.dumps({
                    "id": c["id"], "variant": variant, "label": c["label"], "keyword": c["keyword"], "callee": c["callee"],
                    "reach": a["reach"].noul, "kind": a["kind"].choice, "kind_conf": a["kind"].confidence,
                    "kind_probs": a["kind"].probabilities, "input_tokens": r.usage.input_tokens,
                    "latency_s": round(time.time() - t0, 2)}) + "\n"); out.flush()


def score():
    rows = [json.loads(l) for l in RESULTS.read_text().splitlines() if '"error"' not in l]
    for variant in sorted({r["variant"] for r in rows}):
        rs = [r for r in rows if r["variant"] == variant]
        # for the negated question, P(reach) = 1 - P(not reach)
        p = [(1 - r["reach"]) if variant == "negated" else r["reach"] for r in rs]
        y = [r["label"] for r in rs]
        acc = sum((pi > 0.5) == yi for pi, yi in zip(p, y)) / len(rs)
        kind_ok = sum((r["kind"] == "httpx_client_or_request_function") == r["label"] for r in rs) / len(rs)
        amb = sum(0.2 <= pi <= 0.8 for pi in p)
        wrong_conf = sum(((pi > 0.5) != yi) and (pi < 0.2 or pi > 0.8) for pi, yi in zip(p, y))
        tok = sum(r["input_tokens"] for r in rs) / len(rs); lat = sum(r["latency_s"] for r in rs) / len(rs)
        print(f"{variant:9s} n={len(rs):3d}  noul acc@0.5={acc:.3f}  choice acc={kind_ok:.3f}  "
              f"ambiguous(0.2-0.8)={amb:2d}  confidently-wrong={wrong_conf:2d}  avg tokens={tok:.0f}  avg {lat:.2f}s")
        # calibration buckets
        buckets = {}
        for pi, yi in zip(p, y):
            b = min(int(pi * 5), 4); buckets.setdefault(b, []).append(yi)
        print("           calibration " + "  ".join(f"[{b/5:.1f}-{(b+1)/5:.1f}] n={len(v)} hit={sum(v)/len(v):.2f}"
                                                   for b, v in sorted(buckets.items())))
        for r, pi in zip(rs, p):
            if (pi > 0.5) != r["label"]:
                print(f"           MISS {r['id']}  callee={r['callee']}  label={r['label']}  p={pi:.2f}  kind={r['kind']}")


if __name__ == "__main__":
    args = sys.argv[1:] or ["plain"]
    if args != ["score"]:
        run(args)
    score()
