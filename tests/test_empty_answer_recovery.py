from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from vishwakarma.core import llm as llm_mod
from vishwakarma.core.engine import InvestigationEngine
from vishwakarma.core.llm import LLMConfig, VishwakarmaLLM, _strip_think


def _chunk(content=None, reasoning=None, finish=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _llm():
    return VishwakarmaLLM(LLMConfig(model="openai/m", api_key="k"))


def test_strip_think_handles_closed_and_bare_markers():
    assert _strip_think("<think>a</think>answer") == "answer"
    assert _strip_think("plan plan</think># RCA") == "# RCA"
    assert _strip_think("plain") == "plain"


def test_stream_leaves_content_empty_when_only_reasoning():
    chunks = [_chunk(reasoning="step one "), _chunk(reasoning="step two", finish="stop")]
    with patch.object(llm_mod, "completion", return_value=iter(chunks)):
        events = list(_llm().stream([{"role": "user", "content": "x"}]))
    done = [e for e in events if e["type"] == "analysis_done"]
    assert done and done[0]["content"] == ""


def test_stream_prefers_content_over_reasoning():
    chunks = [_chunk(reasoning="thinking"), _chunk(content="# RCA", finish="stop")]
    with patch.object(llm_mod, "completion", return_value=iter(chunks)):
        events = list(_llm().stream([{"role": "user", "content": "x"}]))
    done = [e for e in events if e["type"] == "analysis_done"]
    assert done[0]["content"] == "# RCA"


def test_parse_response_falls_back_to_reasoning():
    msg = SimpleNamespace(content="", reasoning_content="why</think># RCA", tool_calls=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)
    assert _llm()._parse_response(resp).content == "# RCA"


def test_synthesize_final_skips_empty_models():
    llm = VishwakarmaLLM(LLMConfig(
        model="openai/main", fast_model="openai/fast",
        fast_fallbacks=["openai/fast-fb"], model_fallbacks=["openai/large"], api_key="k"))
    assert llm._final_chain() == ["openai/main", "openai/fast-fb", "openai/large", "openai/fast"]

    def reply(content, reasoning=""):
        msg = SimpleNamespace(content=content, reasoning_content=reasoning)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    answers = {"openai/main": reply("", "plan only"), "openai/fast-fb": reply(""), "openai/large": reply("# RCA " + "x" * 100)}
    with patch.object(llm, "_call_with_fallback", side_effect=lambda models, **kw: answers[models[0]]):
        assert llm.synthesize_final([{"role": "user", "content": "x"}]).startswith("# RCA")


def test_synthesize_final_returns_empty_when_all_fail():
    llm = _llm()
    with patch.object(llm, "_call_with_fallback", side_effect=RuntimeError("down")):
        assert llm.synthesize_final([{"role": "user", "content": "x"}]) == ""


def _engine(final_text):
    llm = MagicMock()
    llm.stream.side_effect = lambda *a, **k: iter([{"type": "analysis_done", "content": ""}])
    llm.synthesize_final.return_value = final_text
    llm.build_meta.return_value = None
    executor = MagicMock()
    executor.toolsets = []
    return InvestigationEngine(llm=llm, executor=executor, max_steps=5), llm


def _final_content(engine):
    events = list(engine.stream_investigate(question="why?"))
    return [e for e in events if e["type"] == "done"][-1]["content"]


def test_engine_recovers_blank_final_answer():
    engine, llm = _engine("# Recovered RCA " + "y" * 100)
    assert _final_content(engine).startswith("# Recovered RCA")
    llm.synthesize_final.assert_called_once()


def test_engine_placeholder_when_synthesis_empty():
    engine, _ = _engine("")
    assert "no written analysis" in _final_content(engine)


def test_thinking_flag_not_sent_by_default(monkeypatch):
    monkeypatch.delenv("VK_DISABLE_THINKING", raising=False)
    assert llm_mod._thinking_off_body() is None


def test_thinking_flag_sent_when_env_set(monkeypatch):
    monkeypatch.setenv("VK_DISABLE_THINKING", "true")
    body = llm_mod._thinking_off_body()
    assert body["chat_template_kwargs"]["enable_thinking"] is False


def test_stream_request_has_no_thinking_flag_by_default(monkeypatch):
    monkeypatch.delenv("VK_DISABLE_THINKING", raising=False)
    seen = {}

    def fake_completion(**kw):
        seen.update(kw)
        return iter([_chunk(content="ok", finish="stop")])

    with patch.object(llm_mod, "completion", side_effect=fake_completion):
        list(_llm().stream([{"role": "user", "content": "x"}]))
    assert "extra_body" not in seen


def test_engine_strips_narration_before_first_heading():
    llm = MagicMock()
    llm.stream.side_effect = lambda *a, **k: iter([{
        "type": "analysis_done",
        "content": "All evidence is in. Writing the final RCA.\n\n---\n\n## Root Cause\nredis is full " + "z" * 100}])
    executor = MagicMock()
    executor.toolsets = []
    engine = InvestigationEngine(llm=llm, executor=executor, max_steps=5)
    llm.synthesize_final.return_value = "unused"
    events = list(engine.stream_investigate(question="why?"))
    content = [e for e in events if e["type"] == "done"][-1]["content"]
    assert content.startswith("## Root Cause")
