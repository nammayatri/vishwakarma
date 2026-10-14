"""Slack bot self-identification must match the actual configured app per
deployment (GCP=Argus, AWS=Sage) — hardcoding "Sage" was wrong on GCP pods,
where the Slack app users actually mention is @Argus, not @sage."""
from vishwakarma.bot.slack import _persona_name, _help_text


class _FakeCfg:
    def __init__(self, cloud):
        self.cloud = cloud


def test_persona_is_argus_on_gcp():
    assert _persona_name(_FakeCfg("gcp")) == "Argus"


def test_persona_is_sage_elsewhere():
    assert _persona_name(_FakeCfg("aws")) == "Sage"
    assert _persona_name(_FakeCfg("")) == "Sage"


def test_help_text_reflects_gcp_persona():
    text = _help_text(_FakeCfg("gcp"))
    assert "Argus" in text
    assert "@argus debug" in text
    assert "@sage" not in text.lower()


def test_help_text_reflects_aws_persona():
    text = _help_text(_FakeCfg("aws"))
    assert "Sage" in text
    assert "@sage debug" in text
