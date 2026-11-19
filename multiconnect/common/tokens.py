"""Cheap token estimation (no tokenizer download needed). This is an ESTIMATE:
prose ~4 chars/token, code ~3.2 chars/token, non-ASCII text counts heavier."""
from __future__ import annotations

CODE_HINTS = ("func ", "var ", "extends ", "{", "}", ";", "=", "(", ")", "\t")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    n = len(text)
    non_ascii = sum(1 for c in text if ord(c) > 127)
    sample = text[:4000]
    code_score = sum(sample.count(h) for h in CODE_HINTS) / max(1, len(sample)) * 100
    ratio = 3.2 if code_score > 3 else 4.0
    est = (n - non_ascii) / ratio + non_ascii / 1.5
    return max(1, int(est))
