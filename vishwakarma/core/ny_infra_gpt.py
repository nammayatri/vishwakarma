"""ny-infra-gpt streaming client — parses its SSE event stream."""
import json


def parse_sse_line(line: str) -> dict | None:
    """One SSE line ('data: {...}') -> the parsed event dict, or None for
    anything else (blank lines, comments, non-JSON payloads)."""
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload:
        return None
    try:
        return json.loads(payload)
    except ValueError:
        return None


def build_question(question: str, thread_context: str = "") -> str:
    """Prefix `question` with prior thread context — only meant for the first
    turn in a thread; later turns rely on conversation_id instead."""
    if not thread_context:
        return question
    ctx = thread_context
    if len(ctx) > 8000:
        ctx = ctx[:4000] + "\n...(truncated)...\n" + ctx[-4000:]
    return f"## Slack thread context so far\n{ctx}\n\n## Question\n{question}"


def stream_ask(config, question: str, conversation_id: int | None = None, post_fn=None):
    """Yields parsed SSE events from POST /ask/stream. post_fn(url, **kwargs)
    is injectable for tests; defaults to a real streaming requests.post."""
    cfg = config.ny_infra_gpt
    if not (cfg.get("enabled") and cfg.get("token")):
        return
    if post_fn is None:
        import requests
        post_fn = requests.post
    body = {"question": question}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    resp = post_fn(
        f"{cfg['url'].rstrip('/')}/ask/stream",
        json=body,
        headers={"Authorization": f"Bearer {cfg['token']}"},
        stream=True,
        # Per-read timeout, not total duration — a slow answer stays open as
        # long as some data keeps arriving; only real silence trips this.
        timeout=cfg.get("timeout", 180),
    )
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        event = parse_sse_line(line)
        if event is not None:
            yield event
