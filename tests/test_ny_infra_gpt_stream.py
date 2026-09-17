"""ny-infra-gpt SSE stream parsing + request assembly."""
from vishwakarma.core.ny_infra_gpt import parse_sse_line, build_question, stream_ask


def test_parse_sse_line_parses_data_lines():
    assert parse_sse_line('data: {"type": "run", "run_id": "abc"}') == {"type": "run", "run_id": "abc"}


def test_parse_sse_line_ignores_non_data():
    assert parse_sse_line("") is None
    assert parse_sse_line(": comment") is None
    assert parse_sse_line("event: ping") is None


def test_parse_sse_line_ignores_malformed_json():
    assert parse_sse_line("data: not json") is None


def test_build_question_no_context_passthrough():
    assert build_question("how many pods?") == "how many pods?"


def test_build_question_prepends_context():
    out = build_question("how many pods?", "alice: pods are down\nbob: investigating")
    assert "## Slack thread context so far" in out
    assert "pods are down" in out
    assert "## Question\nhow many pods?" in out


def test_build_question_truncates_long_context():
    long_ctx = "x" * 20000
    out = build_question("q", long_ctx)
    assert "...(truncated)..." in out
    assert len(out) < len(long_ctx)


class _FakeResp:
    def __init__(self, lines, status_ok=True):
        self._lines = lines
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("boom")

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


class _FakeCfg:
    def __init__(self, **overrides):
        self.ny_infra_gpt = {
            "enabled": True, "url": "http://nyinfragpt.atlas.svc.cluster.local",
            "token": "tok-123", "timeout": 60,
        }
        self.ny_infra_gpt.update(overrides)


def test_stream_ask_yields_parsed_events_in_order():
    lines = [
        'data: {"type": "run", "run_id": "r1"}',
        "",
        'data: {"type": "stage", "stage": "selecting"}',
        'data: {"type": "answer", "conversation_id": 7, "answer": "42 pods"}',
    ]
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        return _FakeResp(lines)

    events = list(stream_ask(_FakeCfg(), "how many pods?", post_fn=fake_post))
    assert [e["type"] for e in events] == ["run", "stage", "answer"]
    assert events[-1]["conversation_id"] == 7
    url, kw = calls[0]
    assert url.endswith("/ask/stream")
    assert kw["headers"]["Authorization"] == "Bearer tok-123"
    assert kw["json"] == {"question": "how many pods?"}


def test_stream_ask_includes_conversation_id_when_given():
    calls = []

    def fake_post(url, **kw):
        calls.append(kw)
        return _FakeResp([])

    list(stream_ask(_FakeCfg(), "follow up", conversation_id=45, post_fn=fake_post))
    assert calls[0]["json"] == {"question": "follow up", "conversation_id": 45}


def test_stream_ask_yields_nothing_when_disabled():
    calls = []

    def fake_post(url, **kw):
        calls.append(kw)
        return _FakeResp([])

    events = list(stream_ask(_FakeCfg(enabled=False), "q", post_fn=fake_post))
    assert events == [] and calls == []


def test_stream_ask_propagates_http_error():
    def fake_post(url, **kw):
        return _FakeResp([], status_ok=False)

    import pytest
    with pytest.raises(RuntimeError):
        list(stream_ask(_FakeCfg(), "q", post_fn=fake_post))
