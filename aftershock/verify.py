"""The Claim Engine: typed questions over rendered evidence, answered by TypeSafe's Jev.

Two claims per candidate span, asked in one request:
  reach  (Noul)   does the consumer actually use the changed API / parameter, not a look-alike
  break  (Choice) breaks_consumer / compatible / unclear
Thresholds are the plan's starting hypotheses; calibrate them on labelled data before trusting them."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

try:                       # the model is optional: `pip install "aftershock[model]"`; without it, rules decide alone
    from typesafe_sdk import (Choice, Noul, TypeSafeAPIConnectionError, TypeSafeAPITimeoutError, TypeSafeClient,
                              TypeSafeInternalServerError, TypeSafeRateLimitError)
    TRANSIENT = (TypeSafeAPIConnectionError, TypeSafeAPITimeoutError, TypeSafeRateLimitError, TypeSafeInternalServerError)
except ImportError:
    TypeSafeClient = None
    TRANSIENT = ()

REACH_Q = ("Does `consumer.code` use `change.changed_object` of the library `change.library` as described in "
           "`change.finding` — for a parameter, passing exactly that keyword to that object (also via **kwargs, partial, "
           "a wrapper, an alias or a subclass); for a removed object, importing, referencing, calling or subclassing it? "
           "The same name on a different class, function or library does not count.")
BREAK_Q = ("`change.finding` describes one API change in `change.library` between `change.old_version` and "
           "`change.new_version`. Does that change break `consumer.code` on upgrade — an import error, attribute error, "
           "TypeError from a missing or misplaced argument, an override with the wrong signature — or silently change "
           "its behaviour?")
BREAK_CRITERIA = {
    "breaks_consumer": {"what": "the consumer uses the changed API in a way the new version rejects or misinterprets",
                        "examples": ["references a removed object", "passes now-keyword-only parameters positionally",
                                     "passes a removed parameter", "overrides a method without its new required parameter"]},
    "compatible": {"what": "the consumer's use is still valid in the new version, or it uses a different, still-valid parameter",
                   "examples": ["passes moved parameters by keyword", "uses a look-alike name from another library",
                                "the override already has the new parameter"]},
    "unclear": "the evidence does not show how the consumer uses the changed API",
}

# Calibrated on 48 rule-labelled public spans (httpx 0.28, click 8.2) and re-calibrated with 44 more from
# an unpublished repository-history benchmark (truth = the commit's own diff); re-checked on seven public
# libraries' histories, spike/CALIBRATION.md. Pooled, plain evidence, 92 model-judged spans: reach >= 0.7 was 64/64 truly
# reached, [0.4,0.6) 1/2, < 0.4 0/26. Break: every truly-reached span answered at confidence >= 0.9.
# 0.7 rather than 0.6 because the [0.6,0.7) band has no observations yet; 0.4 rather than 0.5 because
# a false "not reached" is a silent miss while a false review item is only a cost.
REACH_YES, REACH_NO = 0.7, 0.4
BREAK_AUTO = 0.8


@dataclass
class Verdict:
    reach: float
    break_choice: str
    break_confidence: float
    break_probabilities: dict
    input_tokens: int
    ambiguous: bool = False   # see reach.Span.ambiguous: a "breaks" answer is capped to needs_review

    @property
    def status(self) -> str:
        if self.reach < REACH_NO:
            return "not_reached"
        if self.reach < REACH_YES:
            return "needs_review"
        if self.break_confidence < BREAK_AUTO:
            return "needs_review"
        if self.ambiguous and self.break_choice == "breaks_consumer":
            return "needs_review"
        return self.break_choice


def cap_ambiguous(spans, verdicts):
    """Model verdicts on ambiguous spans (unknown receiver, same-named member defined by the consumer) never
    verify a break on their own: the deciding fact — the receiver's type — is not in the evidence."""
    import dataclasses
    return [dataclasses.replace(v, ambiguous=True) if s.ambiguous else v for s, v in zip(spans, verdicts)]


SKIPPED = Verdict(0.5, "unclear", 0.0, {}, 0)   # budget cap hit: lands in needs_review, never in a verdict


def verify(states: list[dict], client: TypeSafeClient | None = None, budget=None) -> list[Verdict]:
    """One request per state. With a Budget, calls stop at the cap and the rest return SKIPPED."""
    if client is None and (TypeSafeClient is None or not os.environ.get("TYPESAFE_API_KEY")):
        # rules only: what the rules did not decide goes to a person, exactly as when the budget cap is hit
        if states:
            why = ('the model extra is not installed (pip install "aftershock[model]")' if TypeSafeClient is None
                   else "TYPESAFE_API_KEY is not set")
            print(f"aftershock: {why} — {len(states)} span(s) the rules could not decide are left for review, "
                  f"none judged by the model", file=sys.stderr)
        return [SKIPPED] * len(states)
    own = client is None
    client = client or TypeSafeClient()
    out = []
    failed = streak = 0
    try:
        for st in states:
            if budget is not None and not budget.allow():
                out.append(SKIPPED)
                continue
            if streak >= 3:                  # the service is unreachable: stop calling, leave the rest for review
                out.append(SKIPPED); failed += 1
                continue
            try:
                r = client.system_one(state=st, questions={
                    "reach": Noul(instructions=REACH_Q),
                    "break": Choice(instructions=BREAK_Q, criteria=BREAK_CRITERIA),
                })
            except TRANSIENT as e:           # a dropped connection is not a verdict: review, exactly like the budget cap
                out.append(SKIPPED); failed += 1; streak += 1
                last = e
                continue
            streak = 0
            if budget is not None:
                budget.record(r.usage.input_tokens)
            b = r.answers["break"]
            out.append(Verdict(r.answers["reach"].noul, b.choice, b.confidence, dict(b.probabilities), r.usage.input_tokens))
    finally:
        if own:
            client.close()
    if failed:
        print(f"aftershock: {failed} model call(s) failed ({type(last).__name__}) — those spans are left for review", file=sys.stderr)
    return out
