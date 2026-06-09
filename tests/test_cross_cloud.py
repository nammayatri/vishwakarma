"""
Cross-cloud synthesizer tests — findings storage, both-present detection,
single-claim synthesis, and the merge.

Run:  pytest tests/test_cross_cloud.py -v
"""
import sys
import tempfile
import threading

import pytest


def _reset():
    for mod in list(sys.modules):
        if mod.startswith("vishwakarma.storage"):
            del sys.modules[mod]


@pytest.fixture()
def db():
    _reset()
    from vishwakarma.storage import db as dbmod
    dbmod.init_db(db_path=tempfile.mktemp(suffix=".db"))
    return dbmod


def test_write_and_get_findings(db):
    from vishwakarma.core import cross_cloud as cc
    assert not cc.both_present("inc-1")
    cc.write_finding("inc-1", "aws", "AWS: OnConfirm lands here", {"x": 1})
    assert not cc.both_present("inc-1")          # only one half
    cc.write_finding("inc-1", "gcp", "GCP: polling runs here")
    assert cc.both_present("inc-1")

    findings = cc.get_findings("inc-1")
    assert [f["cloud"] for f in findings] == ["aws", "gcp"]
    assert findings[0]["meta"]["x"] == 1


def test_write_finding_upserts(db):
    from vishwakarma.core import cross_cloud as cc
    cc.write_finding("inc-2", "aws", "first")
    cc.write_finding("inc-2", "aws", "second")
    findings = cc.get_findings("inc-2")
    assert len(findings) == 1 and findings[0]["rca"] == "second"


def test_claim_synthesis_is_single_winner(db):
    from vishwakarma.core import cross_cloud as cc
    cc.write_finding("inc-3", "aws", "a")
    cc.write_finding("inc-3", "gcp", "b")

    winners = []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        if cc.claim_synthesis("inc-3", f"w{i}"):
            winners.append(i)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(winners) == 1, f"expected exactly one synthesizer, got {len(winners)}"
    # second call by anyone is rejected
    assert cc.claim_synthesis("inc-3", "late") is False


def test_synthesize_merges_halves(db):
    from vishwakarma.core import cross_cloud as cc

    class FakeLLM:
        def __init__(self):
            self.prompt = None
        def summarize(self, prompt):
            self.prompt = prompt
            return "UNIFIED RCA: cross-cloud forwarding gap"

    cc.write_finding("inc-4", "aws", "AWS half: OnConfirm")
    cc.write_finding("inc-4", "gcp", "GCP half: polling")
    llm = FakeLLM()
    out = cc.synthesize(llm, "Drainer lag", cc.get_findings("inc-4"))
    assert "UNIFIED RCA" in out
    # both halves + cloud labels were given to the model
    assert "AWS half: OnConfirm" in llm.prompt and "GCP half: polling" in llm.prompt
    assert "AWS" in llm.prompt and "GCP" in llm.prompt


def test_synthesize_falls_back_on_llm_failure(db):
    from vishwakarma.core import cross_cloud as cc

    class BrokenLLM:
        def summarize(self, prompt):
            raise RuntimeError("down")

    cc.write_finding("inc-5", "aws", "A-side")
    cc.write_finding("inc-5", "gcp", "G-side")
    out = cc.synthesize(BrokenLLM(), "X", cc.get_findings("inc-5"))
    assert "A-side" in out and "G-side" in out   # concatenation fallback
