"""
Argus bot tests — trigger matching, noise filter, dispatch payload, dedup.

Pure handler tests: no Slack SDK, no LLM — dispatch and classify are injected.

Run:  pytest tests/test_argus_bot.py -v
"""
import pytest

from vishwakarma.bot.argus import ArgusBot, make_classifier

MRE = "S030U6CJU8M"
ARGUS = "U0ARGUSBOT1"


class Recorder:
    def __init__(self, classify_result=True, dispatch_error=None):
        self.dispatched: list[dict] = []
        self.said: list[tuple] = []
        self.classify_result = classify_result
        self.classified: list[str] = []
        self.dispatch_error = dispatch_error

    def dispatch(self, payload):
        if self.dispatch_error:
            raise self.dispatch_error
        self.dispatched.append(payload)
        return "inc-123"

    def classify(self, text):
        self.classified.append(text)
        return self.classify_result

    def say(self, channel, ts, text):
        self.said.append((channel, ts, text))


def make_bot(rec: Recorder) -> ArgusBot:
    return ArgusBot(mre_group_id=MRE, bot_user_id=ARGUS,
                    dispatch=rec.dispatch, classify=rec.classify, say=rec.say)


def ev(text, ts="1.0", channel="C1", user="U_HUMAN", **kw):
    return {"text": text, "ts": ts, "channel": channel, "user": user, **kw}


# ── Triggering ────────────────────────────────────────────────────────────────

def test_mre_mention_triggers_investigation():
    rec = Recorder()
    bot = make_bot(rec)
    action = bot.handle_message(ev(
        f"<!subteam^{MRE}> <@U123> Kindly check. Issue: iOS drivers getting "
        f"OA screens, they must click to accept."))
    assert action == "investigate"
    assert len(rec.dispatched) == 1
    p = rec.dispatched[0]
    assert p["source"] == "slack-argus"
    assert "iOS drivers" in p["description"]
    assert "subteam" not in p["title"]          # mentions stripped
    assert p["labels"]["slack_channel"] == "C1"


def test_direct_argus_mention_skips_filter():
    rec = Recorder(classify_result=False)        # filter would say non-issue
    bot = make_bot(rec)
    action = bot.handle_message(ev(f"<@{ARGUS}> drainer lag climbing again"))
    assert action == "investigate"               # direct mention = forced
    assert rec.classified == []                  # filter not even consulted


def test_debug_keyword_forces():
    rec = Recorder(classify_result=False)
    bot = make_bot(rec)
    action = bot.handle_message(ev(f"<!subteam^{MRE}> debug CustomerDrainerLagIncreasing"))
    assert action == "investigate"
    assert rec.classified == []


def test_untagged_message_ignored():
    rec = Recorder()
    bot = make_bot(rec)
    assert bot.handle_message(ev("the drainer is on fire help")) == "ignore"
    assert rec.dispatched == []


def test_bot_messages_ignored():
    rec = Recorder()
    bot = make_bot(rec)
    assert bot.handle_message(
        ev(f"<!subteam^{MRE}> alarm text", bot_id="B0CLOUDWATCH")) == "ignore"


def test_other_subteam_ignored():
    rec = Recorder()
    bot = make_bot(rec)
    assert bot.handle_message(ev("<!subteam^SOTHERGROUP> check this")) == "ignore"


# ── Noise filter ──────────────────────────────────────────────────────────────

def test_mre_non_issue_stays_quiet():
    rec = Recorder(classify_result=False)
    bot = make_bot(rec)
    action = bot.handle_message(ev(f"<!subteam^{MRE}> thanks for the quick fix yesterday!"))
    assert action == "quiet"
    assert rec.dispatched == []
    assert rec.classified and "thanks" in rec.classified[0]


def test_filter_failure_fails_open():
    rec = Recorder()
    bot = make_bot(rec)
    def boom(_):
        raise RuntimeError("llm down")
    bot.classify = boom
    action = bot.handle_message(ev(f"<!subteam^{MRE}> something seems broken"))
    assert action == "investigate"               # fail open: investigate


# ── Payload details ───────────────────────────────────────────────────────────

def test_screenshot_urls_captured():
    rec = Recorder()
    bot = make_bot(rec)
    bot.handle_message(ev(
        f"<!subteam^{MRE}> OA screen issue, see screenshot",
        files=[{"mimetype": "image/jpeg", "url_private": "https://files.slack/x.jpg"},
               {"mimetype": "text/plain", "url_private": "https://files.slack/y.txt"}]))
    raw = rec.dispatched[0]["raw"]
    assert raw["image_urls"] == ["https://files.slack/x.jpg"]   # images only


def test_thread_context_preserved():
    rec = Recorder()
    bot = make_bot(rec)
    bot.handle_message(ev(f"<!subteam^{MRE}> issue in thread", ts="9.9", thread_ts="5.5"))
    labels = rec.dispatched[0]["labels"]
    assert labels["slack_ts"] == "9.9" and labels["slack_thread_ts"] == "5.5"


def test_duplicate_event_ignored():
    rec = Recorder()
    bot = make_bot(rec)
    msg = ev(f"<!subteam^{MRE}> pods crashing", ts="7.7")
    assert bot.handle_message(msg) == "investigate"
    assert bot.handle_message(msg) == "ignore"   # same ts = same message
    assert len(rec.dispatched) == 1


def test_dispatch_failure_reports_in_thread():
    rec = Recorder(dispatch_error=RuntimeError("already running"))
    bot = make_bot(rec)
    action = bot.handle_message(ev(f"<!subteam^{MRE}> drainer lag again", ts="3.3"))
    assert action == "quiet"
    assert rec.said and "couldn't start" in rec.said[0][2]


# ── Default classifier prompt behavior ────────────────────────────────────────

def test_make_classifier_yes_no_and_fail_open():
    class FakeLLM:
        def __init__(self, answer):
            self.answer = answer
        def summarize(self, prompt):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    assert make_classifier(FakeLLM("YES"))("pods down") is True
    assert make_classifier(FakeLLM("NO"))("thanks all!") is False
    assert make_classifier(FakeLLM("NO — clearly gratitude"))("ty") is False
    assert make_classifier(FakeLLM(RuntimeError("down")))("anything") is True  # fail open
