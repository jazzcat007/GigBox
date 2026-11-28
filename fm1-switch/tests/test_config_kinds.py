"""The yes/no and pick-one setting kinds the Settings page renders as a checkbox and a list."""

import pytest

from fm1_switch import config
from fm1_switch.config import ConfigError, Spec

YESNO = Spec("x_flag", "bool", True, "A flag", "rig")
PICK = Spec("x_mode", "choice", "auto", "A mode", "rig", choices=("auto", "home", "field"))


def test_a_yes_no_setting_takes_true_or_false_and_nothing_else():
    assert config._coerce(YESNO, True) is True
    assert config._coerce(YESNO, False) is False
    for bad in ("yes", 1, 0, None, "true"):
        with pytest.raises(ConfigError):
            config._coerce(YESNO, bad)


def test_a_pick_one_setting_takes_only_a_listed_value():
    assert config._coerce(PICK, "home") == "home"
    for bad in ("sideways", "", 3):
        with pytest.raises(ConfigError):
            config._coerce(PICK, bad)


def test_a_bad_value_in_the_file_falls_back_to_the_default():
    assert config._clamped(YESNO, "maybe") is True
    assert config._clamped(PICK, "sideways") == "auto"


def test_describe_lists_the_choices_for_the_page():
    rows = {r["key"]: r for r in config.describe()["settings"]}
    assert rows["auto_reregister"]["kind"] == "bool"
    assert rows["auto_reregister"]["choices"] == []
