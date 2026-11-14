"""Tesseract's vectors (`dashboard/src/tesseract.vectors.json`, made by `scripts/tesseract_vectors.py`): the file the
dashboard's TypeScript is held to (T2) is what the generator makes byte for byte, and the stored game in it replays,
through the pure rules, through the game module and through `scripts/verify_tesseract.py`, to the numbers it states.
Nothing here touches a relay or shared state."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from emporia.modules import tesseract as T
from emporia.modules import tesseract_core as core

ROOT = Path(__file__).resolve().parent.parent
VECTORS = ROOT / "dashboard" / "src" / "tesseract.vectors.json"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen():
    return _load("tesseract_vectors")


@pytest.fixture(scope="module")
def vectors():
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def test_the_committed_vectors_are_what_the_generator_makes_byte_for_byte(gen):
    made = json.dumps(gen.build(), indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    assert VECTORS.read_text(encoding="utf-8") == made, (
        "the rules changed: regenerate with `.venv/bin/python scripts/tesseract_vectors.py > "
        "dashboard/src/tesseract.vectors.json`")


def test_the_generator_is_deterministic(gen):
    assert json.dumps(gen.build(), sort_keys=True) == json.dumps(gen.build(), sort_keys=True)


def test_the_moves_the_vectors_call_legal_and_illegal_are_so_for_the_module_and_the_core(vectors):
    for mv in vectors["moves"]["legal"]:
        assert core.validate_move(mv, T.RULES_SET)
        assert T.TesseractModule().move_problem({}, mv) == ""
    for mv in vectors["moves"]["illegal"]:
        assert T.TesseractModule().move_problem({}, mv), mv
        with pytest.raises(ValueError):
            core.validate_move(mv, T.RULES_SET)


def test_the_shared_constants_are_the_modules(vectors):
    assert vectors["rules"] == {"width_cap": 12, "open_hi": 12, "unit_cap": 4, "budget": 8, "channels": 4}
    assert (T.RULES_SET.width_cap, T.RULES_SET.open_hi, T.RULES_SET.unit_cap) == (12, 12, 4)
    assert vectors["channels"] == list(core.channel_ids()) and len(vectors["channels"]) == 24
    assert vectors["targets"]["count"] == 726


def test_the_stored_game_replays_through_the_module_to_the_published_numbers(vectors):
    data = vectors["game_data"]
    pl = T.play(data, float("inf"))
    assert pl["opening"] == vectors["opening"] and pl["need"] is None and pl["flow"]["phase"] == "secrets"
    for got, want in zip(pl["epochs"], vectors["epochs"], strict=True):
        assert {k: got["public"][k] for k in ("epoch", "round", "pulses", "dark", "widths", "mix", "pushes")} == \
               {k: want[k] for k in ("epoch", "round", "pulses", "dark", "widths", "mix", "pushes")}
    targets = T._targets(data, pl)
    assert {p: list(t) for p, t in targets.items()} == vectors["targets_dealt"]
    rk = T._ranking(data, pl, targets)
    assert rk["totals"] == vectors["totals"] and rk["index"] == vectors["table_index_dealt"]
    assert rk["order"] == vectors["order"]
    assert T.TesseractModule().rand_problem(data, data["seats"], None) == ""


def test_the_stored_game_replays_through_the_verify_script_to_the_same_numbers(vectors):
    V = _load("verify_tesseract")
    got, problems = V.recompute(vectors["game_data"], bls=False)
    assert problems == []
    assert got["totals"] == vectors["totals"] and got["order"] == vectors["order"]
    assert got["index"] == vectors["table_index_dealt"]
    for g, want in zip(got["epochs"], vectors["epochs"], strict=True):
        assert {k: g[k] for k in ("pulses", "dark", "widths", "mix", "pushes")} == \
               {k: want[k] for k in ("pulses", "dark", "widths", "mix", "pushes")}
        assert {p: v["points"] for p, v in got["points"][g["epoch"] - 1].items()} == \
               {p: v["points"] for p, v in want["points"].items()}


def test_a_changed_number_in_the_vectors_game_is_found(vectors):
    import copy
    data = copy.deepcopy(vectors["game_data"])
    data["opens"]["3"]["agent-b"]["move"]["push"]["XYZ>XY"] = 1
    assert T.TesseractModule().rand_problem(data, data["seats"], None)
    data = copy.deepcopy(vectors["game_data"])
    r = data["rand"]["reveals"]
    r["agent-a"], r["agent-b"] = r["agent-b"], r["agent-a"]
    assert _load("verify_tesseract").recompute(data, bls=False)[1]
