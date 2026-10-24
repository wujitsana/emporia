"""P133: game modules a relay operator adds — loaded explicitly, guarded, rules served."""
import sys
import textwrap

import pytest

import emporia.relay_server  # noqa: F401  (registers the bundled modules)
from emporia import module_sdk
from emporia.module_sdk import MODULE_REGISTRY, load_operator_modules, register_module

_GAME = '''
from emporia.module_sdk import (STAKE_WAGER, InteractionModule, SessionResult,
                                SessionState, register_module)

@register_module
class Coin(InteractionModule):
    MODULE_TYPE = "{mt}"
    DECLARES_WINNER = True
    STAKE_MODEL = STAKE_WAGER
    RULES = "Call heads or tails."
    def initial_state(self, participants, config):
        return SessionState(data={{}}, current_agent=participants[0], step_number=0,
                            metadata={{"participants": participants}})
    def validate_action(self, state, action):
        return True, ""
    def apply_action(self, state, action):
        return SessionResult(success=True, new_state=state)
    def is_terminal(self, state):
        return False, {{}}
'''


@pytest.fixture
def module_dir(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    added = []

    def write(name, mt):
        (tmp_path / f"{name}.py").write_text(textwrap.dedent(_GAME.format(mt=mt)))
        added.append((name, mt))
        return name
    yield write
    for name, mt in added:
        sys.modules.pop(name, None)
        MODULE_REGISTRY.pop(mt, None)


def test_an_operators_game_loads_and_serves_its_rules(module_dir):
    name = module_dir("acme_coin", "acme:coin:v1")
    assert load_operator_modules(f" {name} ,") == ["acme:coin:v1"]
    [listed] = [m for m in module_sdk.list_available_modules() if m["module_type"] == "acme:coin:v1"]
    assert listed["rules"] == "Call heads or tails."
    assert (listed["bundled"], listed["declares_winner"], listed["stake_model"]) == \
        (False, True, "wager")


def test_an_operator_module_cannot_take_the_bundled_namespace(module_dir):
    name = module_dir("acme_fake_chess", "emporia:coinflip:v1")
    with pytest.raises(ValueError, match="reserved"):
        load_operator_modules(name)
    assert "emporia:coinflip:v1" not in MODULE_REGISTRY


def test_a_module_type_is_registered_once():
    """A second class under a taken name used to replace it silently — a
    module could have swapped out the chess that decides who is paid."""
    chess = MODULE_REGISTRY["emporia:chess:v1"]

    class Impostor(chess):
        pass
    with pytest.raises(ValueError, match="already registered"):
        register_module(Impostor)
    assert MODULE_REGISTRY["emporia:chess:v1"] is chess


@pytest.mark.parametrize("bad", ["chess", "Acme:coin:v1", "acme:coin", "acme:coin:1"])
def test_module_types_have_one_shape(bad):
    cls = type("Bad", (MODULE_REGISTRY["emporia:research:v1"],), {"MODULE_TYPE": bad})
    with pytest.raises(ValueError, match="namespace:name:v1"):
        register_module(cls)


def test_a_configured_module_that_registers_nothing_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "acme_empty.py").write_text("x = 1\n")
    with pytest.raises(ValueError, match="registered no module"):
        load_operator_modules("acme_empty")
    with pytest.raises(ModuleNotFoundError):
        load_operator_modules("acme_missing_pkg")


def test_every_bundled_game_tells_agents_its_rules():
    for m in module_sdk.list_available_modules():
        if m["bundled"]:
            assert len(m["rules"]) > 40, f"{m['module_type']} has no RULES for agents"
