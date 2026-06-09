"""
Evaluation + confidence-calibration harness.

Runs the agent against a golden set of past incidents with KNOWN root causes,
and scores:
  - RCA precision: does the produced RCA name the known root cause? (keyword
    overlap, optionally semantic when an embeddings provider is configured)
  - confidence calibration: of the RCAs the agent marked HIGH, how many were
    actually correct? (the number that justifies trusting the fix gate)

Golden set is a JSON list:
  [{"title": "...", "labels": {...}, "known_root_cause": "missing index on ...",
    "must_include": ["seqscan", "driver_offers"],   # optional hard keywords
    "known_fix": "CREATE INDEX ..."}]

The runner is injectable so this is testable without a live LLM, and usable
in CI once a golden set exists.
"""
import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_CONF_RE = re.compile(r"confidence[:\s*]*\**\s*(HIGH|MEDIUM|LOW)", re.IGNORECASE)


@dataclass
class CaseResult:
    title: str
    correct: bool
    confidence: str
    matched_terms: list[str]
    missing_terms: list[str]


@dataclass
class EvalReport:
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def precision(self) -> float:
        return (sum(1 for c in self.cases if c.correct) / self.total) if self.total else 0.0

    def calibration(self, level: str = "HIGH") -> float:
        """Of RCAs marked `level`, fraction actually correct."""
        at = [c for c in self.cases if c.confidence == level]
        return (sum(1 for c in at if c.correct) / len(at)) if at else 0.0

    def confidence_counts(self) -> dict:
        out = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "": 0}
        for c in self.cases:
            out[c.confidence if c.confidence in out else ""] += 1
        return out

    def summary(self) -> dict:
        return {
            "total": self.total,
            "precision": round(self.precision, 3),
            "calibration_high": round(self.calibration("HIGH"), 3),
            "calibration_medium": round(self.calibration("MEDIUM"), 3),
            "confidence_counts": self.confidence_counts(),
        }


def extract_confidence(rca_text: str) -> str:
    m = _CONF_RE.search(rca_text or "")
    return m.group(1).upper() if m else ""


def score_case(case: dict, rca_text: str) -> CaseResult:
    """Decide if an RCA correctly names the known root cause."""
    text = (rca_text or "").lower()

    # Hard terms (if given) must ALL appear; else derive terms from the known
    # root cause (content words) and require a majority.
    must = [t.lower() for t in case.get("must_include", [])]
    if must:
        matched = [t for t in must if t in text]
        missing = [t for t in must if t not in text]
        correct = not missing
    else:
        terms = _content_terms(case.get("known_root_cause", ""))
        matched = [t for t in terms if t in text]
        missing = [t for t in terms if t not in text]
        correct = bool(terms) and (len(matched) / len(terms)) >= 0.5

    return CaseResult(
        title=case.get("title", "?"), correct=correct,
        confidence=extract_confidence(rca_text),
        matched_terms=matched, missing_terms=missing)


def run_eval(golden: list[dict], investigate_fn) -> EvalReport:
    """
    investigate_fn(case) -> rca_text. Lets the caller wire the real engine or
    a stub. Returns the scored report.
    """
    report = EvalReport()
    for case in golden:
        try:
            rca = investigate_fn(case)
        except Exception as e:
            log.warning(f"Eval case '{case.get('title')}' failed: {e}")
            rca = ""
        report.cases.append(score_case(case, rca))
    return report


def load_golden(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


# ── helpers ───────────────────────────────────────────────────────────────────

_STOP = {"the", "a", "an", "of", "on", "in", "to", "is", "was", "due", "by",
         "and", "or", "for", "with", "at", "from", "this", "that", "it", "its",
         "caused", "because", "root", "cause"}


def _content_terms(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]{3,}", (text or "").lower())
    seen = []
    for w in words:
        if w not in _STOP and w not in seen:
            seen.append(w)
    return seen[:8]   # the most salient terms
