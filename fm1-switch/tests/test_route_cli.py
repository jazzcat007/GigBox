"""``fm1-switch route``: patch editing from the shell. Running the router needs ALSA and is
checked on the Pi; here only the file-side commands run."""

from __future__ import annotations

import json

import pytest

from fm1_switch import __main__ as cli


@pytest.fixture(autouse=True)
def patches(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_PATCHES", str(tmp_path / "patches.json"))
    monkeypatch.setenv("FM1_ROUTER_STATUS", str(tmp_path / "status.json"))
    return tmp_path / "patches.json"


def test_add_list_show_delete(patches, capsys):
    assert cli.main(["route", "list"]) == 0
    assert "no patches yet" in capsys.readouterr().out
    assert cli.main(["route", "add", "rig", "fm1-a", "fm1-b", "--remap", "5",
                     "--block", "clock,cc", "--range", "36-72", "--transpose", "-12"]) == 0
    assert cli.main(["route", "add", "rig", "fm1-b", "fm1-c", "--channels", "1,2"]) == 0
    saved = json.loads(patches.read_text())["patches"][0]
    assert saved["name"] == "rig" and len(saved["routes"]) == 2
    assert saved["routes"][0] == {"source": "fm1-a", "dest": "fm1-b", "block": ["clock", "cc"],
                                  "remap_to": 5, "transpose": -12, "low_note": 36,
                                  "high_note": 72}
    capsys.readouterr()
    assert cli.main(["route", "list"]) == 0
    assert "rig  (2 routes)" in capsys.readouterr().out
    assert cli.main(["route", "show", "rig"]) == 0
    assert cli.main(["route", "delete", "rig"]) == 0
    assert cli.main(["route", "show", "rig"]) == 1


@pytest.mark.parametrize("argv", [
    ["add", "rig", "fm1-a", "fm1-a"],  # to itself
    ["add", "rig", "fm1-a", "fm1-b", "--remap", "17"],
    ["add", "rig", "fm1-a", "fm1-b", "--remap"],
    ["add", "rig", "fm1-a", "fm1-b", "--bogus", "1"],
    ["add", "rig", "fm1-a", "fm1-b", "--remap", "x"],
    ["add", "bad/name", "fm1-a", "fm1-b"],
])
def test_bad_input_is_refused_with_a_message_and_nothing_is_saved(argv, patches, capsys):
    assert cli.main(["route", *argv]) == 1
    assert "route:" in capsys.readouterr().err
    assert not patches.exists()


def test_status_says_not_running_and_usage_is_exit_2(capsys):
    assert cli.main(["route", "status"]) == 0
    assert '"running": false' in capsys.readouterr().out
    assert cli.main(["route"]) == 2
    assert cli.main(["route", "frobnicate"]) == 2
    assert cli.main(["route", "run", "nope"]) in (1, 2)
