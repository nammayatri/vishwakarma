"""
Eval-harness tests — scoring, confidence extraction, calibration math.

Run:  pytest tests/test_eval_harness.py -v
"""
from vishwakarma.core.eval_harness import (
    extract_confidence, score_case, run_eval, EvalReport, CaseResult)


def test_extract_confidence():
    assert extract_confidence("## Confidence\nHIGH — clear evidence") == "HIGH"
    assert extract_confidence("Confidence: medium") == "MEDIUM"
    assert extract_confidence("**Confidence:** LOW") == "LOW"
    assert extract_confidence("no confidence marker here") == ""


def test_score_case_must_include():
    case = {"title": "RDS", "must_include": ["seqscan", "driver_offers"]}
    ok = score_case(case, "Root cause: seqscan on driver_offers, missing index")
    assert ok.correct and not ok.missing_terms
    bad = score_case(case, "Root cause: high connections")
    assert not bad.correct and set(bad.missing_terms) == {"seqscan", "driver_offers"}


def test_score_case_known_root_cause_majority():
    case = {"title": "X", "known_root_cause": "missing index on driver_offers table"}
    # 'index','driver_offers','table' present → majority of content terms
    good = score_case(case, "The root cause is a missing index on the driver_offers table")
    assert good.correct
    poor = score_case(case, "unrelated redis eviction problem")
    assert not poor.correct


def test_run_eval_and_calibration():
    golden = [
        {"title": "a", "must_include": ["oom"]},
        {"title": "b", "must_include": ["redis"]},
        {"title": "c", "must_include": ["index"]},
    ]
    # a: correct+HIGH, b: wrong+HIGH (overconfident), c: correct+MEDIUM
    answers = {
        "a": "Confidence: HIGH\nRoot cause: pod OOMKilled",
        "b": "Confidence: HIGH\nRoot cause: cpu spike",     # wrong (no 'redis')
        "c": "Confidence: MEDIUM\nRoot cause: missing index",
    }
    report = run_eval(golden, lambda case: answers[case["title"]])

    assert report.total == 3
    assert abs(report.precision - 2 / 3) < 1e-9
    # of 2 HIGH, 1 correct → 0.5 calibration (this is the signal that the gate
    # would be unsafe until calibration improves)
    assert report.calibration("HIGH") == 0.5
    assert report.calibration("MEDIUM") == 1.0
    assert report.confidence_counts()["HIGH"] == 2

    s = report.summary()
    assert s["precision"] == round(2 / 3, 3) and s["calibration_high"] == 0.5


def test_run_eval_handles_investigate_failure():
    golden = [{"title": "x", "must_include": ["y"]}]
    def boom(case):
        raise RuntimeError("engine down")
    report = run_eval(golden, boom)
    assert report.total == 1 and report.cases[0].correct is False


def test_grades():
    full = score_case({"title": "t", "must_include": ["a", "b"]}, "a and b")
    part = score_case({"title": "t", "must_include": ["a", "b"]}, "a only")
    none = score_case({"title": "t", "must_include": ["a", "b"]}, "zzz")
    bad  = score_case({"title": "t", "must_include": ["a"], "harmful_terms": ["kubectl delete"]},
                      "a — then kubectl delete the pod")
    assert (full.grade, part.grade, none.grade, bad.grade) == ("✅", "⚠️", "❌", "🚫")
    assert full.correct and bad.grade == "🚫" and not bad.correct


def test_grades_derived_terms_path_gets_partial_credit():
    # No must_include — terms are derived from known_root_cause; a >=50% match
    # is "correct" but should still grade ⚠️, not ✅, when terms are missing.
    case = {"title": "t", "known_root_cause": "missing index on driver_offers table causing seqscan"}
    partial = score_case(case, "The root cause is a missing index on the table")
    assert partial.correct and partial.grade == "⚠️" and partial.missing_terms

    case_empty = {"title": "t"}
    nothing_to_grade = score_case(case_empty, "some unrelated text")
    assert nothing_to_grade.grade == "❌" and not nothing_to_grade.correct


def test_gate_zero_harmful_hard_fail():
    r = EvalReport()
    r.cases = [CaseResult("a", True, "HIGH", [], [], "✅"),
               CaseResult("b", False, "LOW", [], [], "🚫")]
    assert not r.gate()
    r.cases[1] = CaseResult("b", False, "LOW", [], [], "❌")
    assert r.gate()
