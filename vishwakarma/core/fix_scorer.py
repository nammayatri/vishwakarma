"""
Fix-confidence scoring + gate.

Distinct from RCA confidence: an RCA can be HIGH-confidence while the *fix*
is still risky (large/cross-cutting diff, no covering test). This module
scores a proposed fix and decides the action:

  draft_pr      — high confidence + localized + tests pass → open a DRAFT PR
                  (human reviews/merges; PR creation itself is the GitHub-App
                  step, gated separately)
  propose_only  — otherwise → post the diff + "change these files" to
                  Slack/console, no PR.

Pure scoring — no I/O, no GitHub dependency — so it's testable and usable
now; the pr_create call sits behind it.
"""
from dataclasses import dataclass

# Weighted signals → score in [0, 1].
_W_RCA_HIGH = 0.30          # RCA itself is high-confidence
_W_RCA_MEDIUM = 0.12
_W_EXACT_LINE = 0.20        # the exact offending line was identified
_W_PATTERN = 0.15          # a human-confirmed pattern matched
_W_LOCALIZED = 0.20        # small, localized diff
_W_TESTS = 0.15            # generated/existing tests pass

DRAFT_PR_THRESHOLD = 0.70
LOCALIZED_MAX_FILES = 3
LOCALIZED_MAX_LINES = 120


@dataclass
class FixDecision:
    score: float
    confidence: str          # HIGH | MEDIUM | LOW
    action: str              # draft_pr | propose_only
    reasons: list[str]


def score_fix(
    rca_confidence: str = "",
    exact_line_found: bool = False,
    pattern_matched: bool = False,
    diff_files: int = 0,
    diff_lines: int = 0,
    tests_passed: bool | None = None,
) -> FixDecision:
    """
    tests_passed: True/False once CI runs; None = unknown (not yet validated)
                  — unknown cannot reach draft_pr (we never PR an unvalidated fix).
    """
    score = 0.0
    reasons: list[str] = []

    rc = (rca_confidence or "").upper()
    if rc == "HIGH":
        score += _W_RCA_HIGH
        reasons.append("RCA high-confidence")
    elif rc == "MEDIUM":
        score += _W_RCA_MEDIUM
        reasons.append("RCA medium-confidence")

    if exact_line_found:
        score += _W_EXACT_LINE
        reasons.append("exact line identified")
    if pattern_matched:
        score += _W_PATTERN
        reasons.append("confirmed pattern matched")

    localized = (0 < diff_files <= LOCALIZED_MAX_FILES
                 and 0 < diff_lines <= LOCALIZED_MAX_LINES)
    if localized:
        score += _W_LOCALIZED
        reasons.append(f"localized diff ({diff_files}f/{diff_lines}l)")
    elif diff_files:
        reasons.append(f"broad diff ({diff_files}f/{diff_lines}l)")

    if tests_passed is True:
        score += _W_TESTS
        reasons.append("tests pass")
    elif tests_passed is False:
        reasons.append("tests FAIL")

    confidence = "HIGH" if score >= DRAFT_PR_THRESHOLD else "MEDIUM" if score >= 0.4 else "LOW"

    # Hard gates: never draft-PR an unvalidated fix, a non-localized diff, or
    # one whose tests failed.
    can_pr = (
        score >= DRAFT_PR_THRESHOLD
        and localized
        and tests_passed is True
    )
    action = "draft_pr" if can_pr else "propose_only"
    if action == "propose_only" and score >= DRAFT_PR_THRESHOLD:
        reasons.append("score high but gate not met (tests/diff) — propose only")

    return FixDecision(score=round(score, 3), confidence=confidence,
                       action=action, reasons=reasons)
