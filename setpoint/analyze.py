from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from setpoint.budget import Usage
from setpoint.retry import with_retries

# Absolute POSIX paths -> basename, so the same failure reported from two
# checkouts (or a worktree) fingerprints identically.
_PATH = re.compile(r"(?:/[\w.\-]+){2,}")
# ISO 8601 timestamps (YYYY-MM-DDTHH:MM:SS), which are variable but not
# indicative of different failures — normalize to canonical form.
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
# Bare numbers (line/col numbers, counts, durations, ports, retries).
# Strip only if NOT preceded by word characters (\w = [a-zA-Z0-9_]), to preserve
# entire digit runs in identifiers (sha256, test2, port8080). Negative lookbehind
# on \w prevents regex engine from restarting inside multi-digit identifier suffixes.
# Lookahead is not used to allow stripping numbers with unit suffixes (0.34s).
_NUM = re.compile(r"(?<!\w)\d+(?:\.\d+)?")
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


_ANALYZE_PROMPT = """You are the ANALYZE stage of a closed loop. An iteration just failed its verify gate.

Plan for the iteration:
{plan}

What the executor did:
{summary}

Gate feedback:
{feedback}

Respond with ONLY a JSON object:
{{"category": "<short kebab-case failure class, e.g. import-error>",
  "symptom": "<what the gate observed, one line>",
  "root_cause": "<why it actually happened, one line>",
  "lesson": "<one imperative rule the next plan must respect — name the exact files, paths, or identifiers involved; a lesson without specifics cannot be acted on>"}}"""

_JSON_BLOB = re.compile(r"\{.*\}", re.S)


@dataclass
class Lesson:
    fingerprint: str
    normalized: str
    category: str = ""
    symptom: str = ""
    root_cause: str = ""
    lesson: str = ""


def _fallback(feedback: str) -> Lesson:
    first = feedback.strip().splitlines()[0][:200] if feedback.strip() else ""
    return Lesson(fingerprint=fingerprint(feedback),
                  normalized=normalize_feedback(feedback), symptom=first)


def analyze(client, model: str, plan: str, summary: str,
            feedback: str) -> tuple[Lesson, Usage]:
    """Distill a failed iteration into a lesson. Never raises — any failure
    returns a fingerprint-only fallback so the loop is never blocked."""
    if getattr(client, "is_noop", False):
        return _fallback(feedback), Usage()
    prompt = _ANALYZE_PROMPT.format(plan=plan[:2000], summary=summary[:2000],
                                    feedback=feedback[:4000])
    try:
        resp = with_retries(lambda: client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
        ), attempts=2)
    except Exception:
        return _fallback(feedback), Usage()
    u = resp.usage
    usage = Usage(getattr(u, "prompt_tokens", 0) or 0,
                  getattr(u, "completion_tokens", 0) or 0,
                  getattr(u, "prompt_cache_hit_tokens", 0) or 0)
    m = _JSON_BLOB.search(resp.choices[0].message.content or "")
    if not m:
        return _fallback(feedback), usage
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return _fallback(feedback), usage
    return Lesson(
        fingerprint=fingerprint(feedback),
        normalized=normalize_feedback(feedback),
        category=str(data.get("category", ""))[:60],
        symptom=str(data.get("symptom", ""))[:200],
        root_cause=str(data.get("root_cause", ""))[:300],
        lesson=str(data.get("lesson", ""))[:300],
    ), usage
