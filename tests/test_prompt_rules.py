"""Prompt-rule tests — kit-derived evidence/injection rules must be present."""
from vishwakarma.core.prompt import (
    SYSTEM_INTRO, INVESTIGATION_PHASES, GENERAL_GUIDELINES, build_system_prompt)


def test_untrusted_data_clause():
    low = SYSTEM_INTRO.lower()
    assert "data, not instructions" in low
    assert "alert payload" in low


def test_rca_requires_linked_evidence_and_change_mind():
    assert "link to the tool output" in INVESTIGATION_PHASES
    assert "One observation that would change my mind" in INVESTIGATION_PHASES


def test_blame_crosscheck_rule():
    assert "still happening" in GENERAL_GUIDELINES


def test_rules_survive_build_with_runbook():
    p = build_system_prompt([], runbooks=["do x"], knowledge="kb")
    assert "still happening" in p          # GENERAL_GUIDELINES is unconditional
    assert "Detecting What Changed" not in p  # sanity: WHAT_CHANGED stays runbook-gated


def test_code_fix_does_not_mandate_opening_a_pr():
    low = INVESTIGATION_PHASES.lower()
    assert "do not open a code_session or call propose_fix" in low
    assert "must attempt the actual fix" not in low
    assert "recommending a fix in prose is not enough" not in low
