"""Config file (audit B13): unit count and paths come from one JSON file; env still wins."""
import importlib
import json


def _reload(monkeypatch, tmp_path, cfg):
    f = tmp_path / "config.json"
    if cfg is not None:
        f.write_text(cfg if isinstance(cfg, str) else json.dumps(cfg))
    monkeypatch.setenv("FM1_CONFIG", str(f))
    from fm1_switch import units
    return importlib.reload(units)


def test_default_is_four_units(monkeypatch, tmp_path):
    u = _reload(monkeypatch, tmp_path, None)
    assert u.SLOTS == ("fm1-a", "fm1-b", "fm1-c", "fm1-d")
    assert u.SLOT_LABELS["fm1-c"] == "C"


def test_unit_count_from_file_is_clamped(monkeypatch, tmp_path):
    assert len(_reload(monkeypatch, tmp_path, {"units": 6}).SLOTS) == 6
    assert len(_reload(monkeypatch, tmp_path, {"units": 99}).SLOTS) == 16
    assert len(_reload(monkeypatch, tmp_path, {"units": 0}).SLOTS) == 1


def test_broken_or_odd_file_is_ignored(monkeypatch, tmp_path):
    assert len(_reload(monkeypatch, tmp_path, "{not json").SLOTS) == 4
    assert len(_reload(monkeypatch, tmp_path, {"units": "six"}).SLOTS) == 4
    assert len(_reload(monkeypatch, tmp_path, {"units": True}).SLOTS) == 4
    _reload(monkeypatch, tmp_path, None)


def test_paths_env_beats_file_beats_default(monkeypatch, tmp_path):
    from fm1_switch import library
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"library": str(tmp_path / "fromfile")}))
    monkeypatch.setenv("FM1_CONFIG", str(f))
    monkeypatch.delenv("FM1_LIBRARY", raising=False)
    assert library.library_dir() == tmp_path / "fromfile"
    monkeypatch.setenv("FM1_LIBRARY", str(tmp_path / "fromenv"))
    assert library.library_dir() == tmp_path / "fromenv"
    monkeypatch.delenv("FM1_LIBRARY")
    f.write_text("{}")
    assert str(library.library_dir()).endswith("fm1-firmwares")
