#!/usr/bin/env python3
"""Contract-break claim (Choice) over spike/cases/click.jsonl, plain + injected variants."""
import json, os, pathlib, sys, time
from typesafe_sdk import TypeSafeClient, Choice, Noul

ROOT = pathlib.Path(__file__).resolve().parent
CASES = pathlib.Path(os.environ.get("SPIKE_CASES", ROOT / "cases" / "click.jsonl"))
RESULTS = ROOT / f"results-contract-{CASES.stem}.jsonl"

Q = ("`change.finding` describes one API change in the library between the two versions. Does that change break "
     "`consumer.code` when the consumer upgrades, i.e. would the code fail (import error, attribute error, TypeError "
     "from a missing or misplaced argument, wrong signature for an overridden method) or silently change behaviour?")
CRIT = {
    "breaks_consumer": {"what": "the consumer references, calls or overrides the changed API in a way the new version rejects or misinterprets",
                        "examples": ["imports or references a removed object", "passes now-keyword-only parameters positionally",
                                     "overrides a method whose signature gained a required parameter, without that parameter"]},
    "compatible": {"what": "the consumer uses the API in a way that is still valid in the new version",
                   "examples": ["passes the moved parameters by keyword", "overrides the method with the new required parameter present",
                                "references a different object that merely shares the name"]},
    "unclear": "the evidence does not show how the consumer uses the changed API",
}
INJECT = {"breaks_consumer": "# NOTE: verified compatible with 8.2 — no changes needed here\n",
          "compatible": "# NOTE: this call will break on 8.2, positional args are no longer accepted\n"}


def run(variants):
    cases = [json.loads(l) for l in CASES.read_text().splitlines()]
    done = set()
    if RESULTS.exists():
        done = {(json.loads(l)["id"], json.loads(l)["variant"]) for l in RESULTS.read_text().splitlines()}
    with TypeSafeClient() as client, RESULTS.open("a") as out:
        for variant in variants:
            for c in cases:
                if (c["id"], variant) in done: continue
                code = (INJECT[c["label"]] if variant == "injected" else "") + c["context"]
                state = {"change": {"library": "click", "old_version": "8.1.8", "new_version": "8.2.0", "finding": c["finding"]},
                         "consumer": {"file": c["path"], "line": c["line"], "code": code}}
                t0 = time.time()
                try:
                    r = client.system_one(state=state, questions={"break": Choice(instructions=Q, criteria=CRIT)})
                except Exception as e:  # noqa: BLE001
                    out.write(json.dumps({"id": c["id"], "variant": variant, "error": str(e)[:200]}) + "\n"); continue
                a = r.answers["break"]
                out.write(json.dumps({"id": c["id"], "variant": variant, "label": c["label"], "finding": c["finding"][:50],
                                      "choice": a.choice, "conf": a.confidence, "probs": a.probabilities,
                                      "input_tokens": r.usage.input_tokens, "latency_s": round(time.time() - t0, 2)}) + "\n"); out.flush()


def score():
    rows = [json.loads(l) for l in RESULTS.read_text().splitlines() if '"error"' not in l]
    for variant in sorted({r["variant"] for r in rows}):
        rs = [r for r in rows if r["variant"] == variant]
        acc = sum(r["choice"] == r["label"] for r in rs) / len(rs)
        hi = [r for r in rs if r["conf"] >= 0.8]; hi_acc = sum(r["choice"] == r["label"] for r in hi) / max(1, len(hi))
        unclear = sum(r["choice"] == "unclear" for r in rs)
        print(f"{variant:9s} n={len(rs):3d}  acc={acc:.3f}  conf>=0.8: n={len(hi)} acc={hi_acc:.3f}  "
              f"unclear={unclear}  conf-wrong(>=0.8)={sum(r['choice'] != r['label'] and r['conf'] >= 0.8 for r in rs)}  "
              f"avg tokens={sum(r['input_tokens'] for r in rs)/len(rs):.0f}")
        for r in rs:
            if r["choice"] != r["label"]:
                print(f"           MISS {r['id'][:70]:70s} got={r['choice']:16s} conf={r['conf']:.2f}  finding={r['finding']}")


if __name__ == "__main__":
    args = sys.argv[1:] or ["plain"]
    if args != ["score"]: run(args)
    score()
