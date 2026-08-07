from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

# Absolute POSIX paths -> basename, so the same failure reported from two
# checkouts (or a worktree) fingerprints identically.
_PATH = re.compile(r"(?:/[\w.\-]+){2,}")
# ISO 8601 timestamps (YYYY-MM-DDTHH:MM:SS), which are variable but not
# indicative of different failures — normalize to canonical form.
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
# Bare numbers (line/col numbers, counts, durations, ports, retries).
# Strip only if NOT preceded by letters/underscores, to preserve numbers in
# identifiers (sha256, test2, port8080) which represent different code.
# Lookahead is not used to allow stripping numbers with unit suffixes (0.34s).
_NUM = re.compile(r"(?<![a-zA-Z_])\d+(?:\.\d+)?")
_WS = re.compile(r"\s+")

NEAR_MATCH_RATIO = 0.9


def normalize_feedback(text: str) -> str:
    t = _PATH.sub(lambda m: m.group(0).rsplit("/", 1)[-1], text)
    t = _TIMESTAMP.sub("#-#-#T#:#:#", t)
    t = _NUM.sub("#", t)
    t = _WS.sub(" ", t).strip().lower()
    return t[:2000]


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_feedback(text).encode()).hexdigest()[:12]


def is_repeat(feedback: str, category: str,
              priors: list[tuple[str, str, str]]) -> str | None:
    """priors: (fingerprint, normalized, category) of earlier failures/lessons.
    Returns the matched prior fingerprint, or None."""
    fp = fingerprint(feedback)
    norm = normalize_feedback(feedback)
    for pfp, pnorm, pcat in priors:
        if fp == pfp:
            return pfp
        if (category and pcat and category == pcat and pnorm
                and SequenceMatcher(None, norm, pnorm).ratio() >= NEAR_MATCH_RATIO):
            return pfp
    return None
