"""
Active-verification tool tests — confirm/refute verdicts via a read-only check.

Run:  pytest tests/test_verify.py -v
"""
from vishwakarma.core.models import ToolStatus
from vishwakarma.plugins.toolsets.verify.verify import VerifyToolset


def _ts():
    # allow `echo`/`printf` so the check can run under bash safety
    return VerifyToolset({"bash_config": {"safe_mode": False,
                                          "allow": ["echo", "printf", "true", "grep"]}})


def test_confirmed_when_signal_present():
    ts = _ts()
    out = ts.execute("verify_hypothesis", {
        "hypothesis": "seqscan on driver_offers",
        "check": "echo 'QUERY PLAN: Seq Scan on driver_offers'",
        "expect": "Seq Scan"})
    assert out.status == ToolStatus.SUCCESS, out.error
    assert "VERDICT: CONFIRMED" in str(out.output)
    assert "Seq Scan on driver_offers" in str(out.output)


def test_refuted_when_signal_absent():
    ts = _ts()
    out = ts.execute("verify_hypothesis", {
        "hypothesis": "missing index",
        "check": "echo 'QUERY PLAN: Index Scan using idx_driver'",
        "expect": "Seq Scan"})
    assert "VERDICT: REFUTED" in str(out.output)


def test_expect_absent_inverts():
    ts = _ts()
    # hypothesis confirmed by the ABSENCE of errors
    out = ts.execute("verify_hypothesis", {
        "hypothesis": "no connection errors after the fix",
        "check": "echo 'all healthy'",
        "expect": "ERROR", "expect_absent": True})
    assert "VERDICT: CONFIRMED" in str(out.output)


def test_missing_params():
    ts = _ts()
    out = ts.execute("verify_hypothesis", {"hypothesis": "x"})
    assert out.status == ToolStatus.ERROR


def test_blocked_command_surfaces_error():
    ts = VerifyToolset({"bash_config": {"safe_mode": True, "allow": []}})
    out = ts.execute("verify_hypothesis", {
        "hypothesis": "x", "check": "rm -rf /", "expect": "y"})
    assert out.status == ToolStatus.ERROR   # bash safety refuses rm


def test_registered():
    from vishwakarma.core.toolset_manager import _PYTHON_TOOLSET_REGISTRY
    assert "verify" in _PYTHON_TOOLSET_REGISTRY
