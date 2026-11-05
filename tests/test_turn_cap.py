"""P284: the Hermes plugin caps the turns relay events start in 24 hours."""
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "emporia_turn_cap", Path(__file__).resolve().parents[1] / "hermes_plugin" / "emporia" / "turn_cap.py")
cap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cap)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "_home", lambda: tmp_path)
    monkeypatch.setattr(cap, "_setting", lambda name: "3")
    cap.data_dir(tmp_path).mkdir(parents=True)
    return tmp_path


def test_relay_turns_are_counted_then_blocked_past_the_cap(home):
    assert [cap.pre_llm_call(platform="emporia") for _ in range(3)] == [None] * 3
    out = cap.pre_llm_call(platform="emporia")
    assert out["action"] == "block" and "EMPORIA_DAILY_TURNS" in out["message"] and "cap of 3" in out["message"]
    assert cap.used(home) == 3                       # a blocked turn is not counted


def test_other_platforms_are_never_counted_or_blocked(home):
    for _ in range(5):
        assert cap.pre_llm_call(platform="telegram") is None
        assert cap.pre_llm_call(platform="") is None
    assert cap.used(home) == 0


def test_turns_older_than_a_day_fall_out(home):
    import time
    old = time.time() - cap.WINDOW - 5
    (cap.data_dir(home) / cap.FILE).write_text(json.dumps({"turns": [old, old, old]}))
    assert cap.pre_llm_call(platform="emporia") is None
    assert cap.used(home) == 1


def test_zero_means_no_cap_and_bad_values_fall_back(home, monkeypatch):
    monkeypatch.setattr(cap, "_setting", lambda name: "0")
    for _ in range(10):
        assert cap.pre_llm_call(platform="emporia") is None
    assert not (cap.data_dir(home) / cap.FILE).exists()
    monkeypatch.setattr(cap, "_setting", lambda name: "lots")
    assert cap.cap() == cap.DEFAULT_CAP
    monkeypatch.setattr(cap, "_setting", lambda name: "")
    assert cap.cap() == cap.DEFAULT_CAP


def test_a_broken_file_starts_over(home):
    (cap.data_dir(home) / cap.FILE).write_text("{not json")
    assert cap.pre_llm_call(platform="emporia") is None
    assert cap.used(home) == 1


def test_the_counter_lives_in_plugin_data_and_an_old_one_is_moved_there(home):
    """Catalog rule 13: the plugin writes only its own data in the profile. A counter kept at
    the profile's root before is counted, then moved into plugin-data/emporia."""
    import time
    now = time.time()
    (home / cap.OLD_FILE).write_text(json.dumps({"turns": [now - 10, now - 20]}))
    (home / f".{cap.OLD_FILE}.lock").write_text("")
    assert cap.used(home) == 2
    assert cap.pre_llm_call(platform="emporia") is None
    assert cap.used(home) == 3
    assert len(json.loads((home / "plugin-data" / "emporia" / "turns.json").read_text())["turns"]) == 3
    assert not (home / cap.OLD_FILE).exists() and not (home / f".{cap.OLD_FILE}.lock").exists()
    assert not [p.name for p in home.iterdir() if "turns" in p.name]


def test_the_plugin_registers_the_hook_and_requires_the_hermes_that_has_it():
    root = Path(__file__).resolve().parents[1] / "hermes_plugin"
    assert 'register_hook("pre_llm_call", turn_cap.pre_llm_call)' in (root / "emporia" / "__init__.py").read_text()
    assert 'requires_hermes: ">=0.22.6"' in (root / "emporia" / "plugin.yaml").read_text()
