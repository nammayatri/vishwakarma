"""
Run-until-verified — verification-checkpoint scheduling + prompt.

Run:  pytest tests/test_run_until_verified.py -v
"""
from vishwakarma.core.engine import InvestigationEngine, CHECKPOINT_STEP


def _engine() -> InvestigationEngine:
    # construct without real llm/executor — we only test the scheduling logic
    e = InvestigationEngine.__new__(InvestigationEngine)
    e.run_until_verified = True
    e.verify_after = 12
    e.verify_every = 8
    return e


def test_periodic_verification_schedule():
    e = _engine()
    last = -1
    fired = []
    for step in range(40):
        if e._should_verify(step, last):
            fired.append(step)
            last = step
    # first at 12, then every 8: 12, 20, 28, 36
    assert fired == [12, 20, 28, 36]


def test_no_verify_before_threshold():
    e = _engine()
    assert not any(e._should_verify(s, -1) for s in range(12))


def test_legacy_single_checkpoint_when_disabled():
    e = _engine()
    e.run_until_verified = False
    fired = [s for s in range(40) if e._should_verify(s, -1)]
    assert fired == [CHECKPOINT_STEP]


def test_not_twice_at_same_step():
    e = _engine()
    assert e._should_verify(12, -1) is True
    assert e._should_verify(12, 12) is False   # already injected here


def test_verify_prompt_content():
    e = _engine()
    p = e._verify_prompt(20)
    assert p["role"] == "user"
    text = p["content"].lower()
    assert "verify" in text and "explain all" in text
    assert "confirmed" in text and "root cause" in text
