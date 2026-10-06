"""Render a (finding, span) pair into the JSON state a claim is judged against.

Rules learned in the spike: named fields, no unrelated code (accuracy falls with distractors), and
comments are attacker-controlled — a planted comment drifted answers and halved high-confidence
verdicts — so they are stripped by default."""
from __future__ import annotations

import io
import tokenize

from .reach import Span


def strip_comments(code: str) -> str:
    """Drop `# ...` comments (not docstrings) using the tokenizer; falls back to the input on error."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, SyntaxError):
        return code
    out, last_end = [], (1, 0)
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            continue
        srow, scol = tok.start
        if srow > last_end[0]:
            out.append("\n" * (srow - last_end[0]) + " " * scol)
        elif scol > last_end[1]:
            out.append(" " * (scol - last_end[1]))
        out.append(tok.string)
        last_end = tok.end
    return "".join(out).strip("\n")


def render(span: Span, library: str, old_version: str, new_version: str, keep_comments: bool = False) -> dict:
    code = span.context if keep_comments else strip_comments(span.context)
    return {
        "change": {
            "library": library,
            "old_version": old_version,
            "new_version": new_version,
            "finding": span.finding.describe(),
            "changed_object": span.finding.object_name,
            "parameter": span.finding.parameter,
        },
        "consumer": {"file": span.file, "line": span.line, "code": code},
    }
