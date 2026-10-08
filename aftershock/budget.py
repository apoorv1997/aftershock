"""Hard spend limits for model calls. No call is made past the cap; the report says how many were skipped.

Two limits, both in USD at the list price ($0.042 per million input tokens, output free):
  per run     --budget-usd (default 0.02)
  cumulative  AFTERSHOCK_MAX_SPEND_USD (default 1.00), tracked in ~/.cache/aftershock/spend.jsonl across runs
The TypeSafe console is the only place a limit can be enforced on their side; this is the client side."""
from __future__ import annotations

import json
import os
import pathlib
import time

PRICE_PER_TOKEN = 0.042 / 1_000_000
LEDGER = pathlib.Path.home() / ".cache" / "aftershock" / "spend.jsonl"


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, run_usd: float = 0.02, total_usd: float | None = None):
        self.run_usd = run_usd
        self.total_usd = float(os.environ.get("AFTERSHOCK_MAX_SPEND_USD", 1.00)) if total_usd is None else total_usd
        self.spent_tokens = 0
        self.skipped = 0
        self.prior_usd = self._ledger_total()

    @staticmethod
    def _ledger_total() -> float:
        if not LEDGER.exists():
            return 0.0
        return sum(json.loads(l).get("usd", 0.0) for l in LEDGER.read_text().splitlines() if l.strip())

    @property
    def run_spent_usd(self) -> float:
        return self.spent_tokens * PRICE_PER_TOKEN

    def allow(self, estimate_tokens: int = 800) -> bool:
        """Is one more call (of about `estimate_tokens`) within both caps?"""
        next_usd = (self.spent_tokens + estimate_tokens) * PRICE_PER_TOKEN
        ok = next_usd <= self.run_usd and self.prior_usd + next_usd <= self.total_usd
        if not ok:
            self.skipped += 1
        return ok

    def record(self, input_tokens: int) -> None:
        self.spent_tokens += input_tokens

    def close(self, label: str = "") -> None:
        if self.spent_tokens:
            LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with LEDGER.open("a") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "label": label,
                                    "tokens": self.spent_tokens, "usd": round(self.run_spent_usd, 6)}) + "\n")

    def summary(self) -> str:
        s = (f"model spend this run: {self.spent_tokens:,} tokens = ${self.run_spent_usd:.4f} "
             f"(cap ${self.run_usd:.2f}); cumulative ${self.prior_usd + self.run_spent_usd:.4f} of ${self.total_usd:.2f}")
        if self.skipped:
            s += f"\n  BUDGET CAP HIT: {self.skipped} verifications skipped and reported as 'needs_review'"
        return s
