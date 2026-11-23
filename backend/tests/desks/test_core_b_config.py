"""TA-86: settings with an environment override; a misspelled boolean fails at startup."""
import json

import pytest

from hokma.desks.core import config as cfg


def test_defaults_when_nothing_is_set_TA_86():
    loaded = cfg.load_config({}, {})
    assert loaded["max_debate_rounds"] == 1 and loaded["checkpoint_enabled"] is False and loaded["temperature"] is None
    assert set(loaded.sources.values()) == {"default"}


def test_the_environment_wins_over_the_file_TA_86():
    loaded = cfg.load_config({"max_debate_rounds": 3, "news_article_limit": 5}, {"HOKMA_DESK_MAX_DEBATE_ROUNDS": "2"})
    assert loaded["max_debate_rounds"] == 2 and loaded.sources["max_debate_rounds"] == "env"
    assert loaded["news_article_limit"] == 5 and loaded.sources["news_article_limit"] == "file"


@pytest.mark.parametrize("text,expected", [("true", True), ("1", True), ("YES", True), ("on", True), ("false", False),
                                           ("0", False), ("No", False), (" off ", False)])
def test_booleans_are_read_case_insensitively_TA_86(text, expected):
    assert cfg.load_config({}, {"HOKMA_DESK_CHECKPOINT_ENABLED": text})["checkpoint_enabled"] is expected


def test_a_misspelled_boolean_fails_with_a_clear_message_TA_86():
    with pytest.raises(cfg.ConfigError) as info:
        cfg.load_config({}, {"HOKMA_DESK_CHECKPOINT_ENABLED": "treu"})
    message = str(info.value)
    assert "HOKMA_DESK_CHECKPOINT_ENABLED" in message and "treu" in message and "boolean" in message


@pytest.mark.parametrize("name,value", [("HOKMA_DESK_MAX_DEBATE_ROUNDS", "two"), ("HOKMA_DESK_MAX_DEBATE_ROUNDS", "99"),
                                        ("HOKMA_DESK_TEMPERATURE", "hot"), ("HOKMA_DESK_TEMPERATURE", "5"),
                                        ("HOKMA_DESK_HOLDING_PERIOD_DAYS", "0"), ("HOKMA_DESK_MAX_TOKENS", "-1"),
                                        ("HOKMA_DESK_LLM_MAX_RETRIES", "1.5")])
def test_a_bad_number_fails_at_startup_TA_86(name, value):
    with pytest.raises(cfg.ConfigError) as info:
        cfg.load_config({}, {name: value})
    assert name in str(info.value)


def test_nullable_numbers_and_floats_TA_86():
    loaded = cfg.load_config({}, {"HOKMA_DESK_TEMPERATURE": "0.2", "HOKMA_DESK_MAX_TOKENS": "2048"})
    assert loaded["temperature"] == 0.2 and loaded["max_tokens"] == 2048
    assert cfg.load_config({"temperature": None}, {})["temperature"] is None


def test_an_unknown_key_in_the_file_is_an_error_TA_86():
    with pytest.raises(cfg.ConfigError, match="unknown setting"):
        cfg.load_config({"max_debate_round": 2}, {})


def test_the_settings_file_is_read_and_checked_TA_86(tmp_path):
    path = tmp_path / "desk-settings.json"
    assert cfg.read_settings_file(path) == {}
    path.write_text(json.dumps({"output_language": "Spanish"}), encoding="utf-8")
    assert cfg.load_config(cfg.read_settings_file(path), {})["output_language"] == "Spanish"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(cfg.ConfigError, match="JSON object"):
        cfg.read_settings_file(path)
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(cfg.ConfigError, match="not valid JSON"):
        cfg.read_settings_file(path)


def test_every_setting_has_its_own_environment_variable_TA_86():
    names = [s.env for s in cfg.SETTINGS]
    assert len(set(names)) == len(names) and all(n.startswith("HOKMA_DESK_") for n in names)
