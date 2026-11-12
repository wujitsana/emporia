"""The signed card's operator label, client and declared model; look-alike names; model commitments."""

import time
from pathlib import Path

import pytest


from emporia import identity_card, model_commit, names, operators, wire


def test_a_label_is_op_and_26_base32_characters():
    label = operators.new_label()
    assert label.startswith("op_") and len(label) == 29 and operators.valid_label(label)
    assert operators.new_label() != label
    for bad in ("", "op_short", "OP_" + "a" * 26, "op_" + "1" * 26, "x_" + "a" * 26):
        assert not operators.valid_label(bad)


def test_operator_of_prefers_the_label_then_the_key():
    label = operators.new_label()
    assert operators.operator_of(label, "stable1", "alice") == f"label:{label}"
    assert operators.operator_of("", "stable1", "alice") == "key:stable1"
    assert operators.operator_of(None, None, "alice") == "agent:alice"
    assert operators.operator_of_row({"operator_label": label, "stable_id": "s", "agent_id": "a"}) == f"label:{label}"


def test_the_signed_card_binds_the_label_and_client():
    label = operators.new_label()
    a = wire.signed_registration_payload("ab" * 16, "https://r.example", "alice", "k" * 64, label, "sdk", "0.2.0")
    b = wire.signed_registration_payload("ab" * 16, "https://r.example", "alice", "k" * 64)
    assert a["operator"] == label and a["client"] == {"name": "sdk", "version": "0.2.0"}
    assert a != b and b["operator"] == ""
    assert wire.registration_problem("ab" * 16, "https://r.example", "alice", "k" * 64, label, a["client"]) in (
        "", "public_key is the agent's Ed25519 public key (hex)")
    assert "operator" in wire.registration_problem("ab" * 16, "https://r.example", "alice", "a" * 64, "nope", None)


def test_lookalike_handles_are_refused_and_display_names_cleaned():
    assert names.skeleton("rn-bot") == names.skeleton("m_b0t") == names.skeleton("MBOT")
    assert names.lookalike_problem("a1ice", ["alice", "bob"], ["admin"])
    assert names.lookalike_problem("adm1n", [], ["admin"])
    assert names.lookalike_problem("alice", ["alice"], []) == ""           # itself
    assert names.lookalike_problem("carol", ["alice"], ["admin"]) == ""
    assert names.clean_display_name("  Alice  the Bold ") == "Alice  the Bold"
    for bad in ("x" * 65, "a‮b", "line\nbreak", "see https://evil.example", "www.evil.example", "a\x00b"):
        with pytest.raises(ValueError):
            names.clean_display_name(bad)


def test_model_commit_and_reveal_check():
    salt = model_commit.new_salt()
    c = model_commit.commit("deepseek/v4", salt)
    assert c == model_commit.commit("deepseek/v4", salt) and c != model_commit.commit("deepseek/v4", salt + "x")
    assert not model_commit.commit_problem(c) and model_commit.commit_problem("zz")
    reveals = [{"step": 0, "model": "deepseek/v4", "salt": salt}, {"step": 2, "model": "other", "salt": salt}]
    assert not model_commit.reveals_problem(reveals)
    assert model_commit.reveals_problem([{"step": 0, "model": "x y", "salt": salt}])
    checked = model_commit.check_reveals({0: c, 2: c, 4: c}, reveals)
    assert checked == {0: "ok", 2: "mismatch"}
    assert model_commit.standing({0: c}, {0: "ok"}) == model_commit.REVEALED
    assert model_commit.standing({0: c, 2: c}, {0: "ok", 2: "mismatch"}) == model_commit.MISMATCH
    assert model_commit.standing({0: c, 2: c}, {0: "ok"}) == model_commit.MISSING
    assert model_commit.standing({}, {}) == model_commit.NONE


def test_local_salts_round_trip(tmp_path):
    store = model_commit.LocalSalts(tmp_path / "salts.json")
    store.remember("r|s1", 0, "m", "salt-0000")
    store.remember("r|s1", 2, "m", "salt-0002")
    assert [r["step"] for r in store.recall("r|s1")] == [0, 2]
    assert oct((tmp_path / "salts.json").stat().st_mode & 0o777) == "0o600"
    store.forget("r|s1")
    assert store.recall("r|s1") == []


def test_declared_model_comes_from_the_plugins_record_then_config(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.delenv("EMPORIA_MODEL", raising=False)
    monkeypatch.delenv("EMPORIA_DECLARE_MODEL", raising=False)
    assert identity_card.current_model(home) == ""
    (home / "config.yaml").write_text("model:\n  default: nous/hermes-4\n  provider: nous\n  base_url: https://x.example\n")
    assert identity_card.current_model(home) == "nous/hermes-4"          # public config, never the endpoint
    identity_card.record_model(home, "anthropic/claude-x")
    assert identity_card.current_model(home) == "anthropic/claude-x"
    assert identity_card.current_model(home, now=time.time() + 3 * 86400) == "nous/hermes-4"     # a stale record
    identity_card.record_model(home, "bad name with spaces")
    assert identity_card.current_model(home) == "anthropic/claude-x"
    monkeypatch.setenv("EMPORIA_DOTENV", str(home / ".env"))
    assert identity_card.model_tags() == [["model", "anthropic/claude-x"]]
    assert identity_card.with_model_tag([["t", "x"], ["model", "forged"]]) == [["t", "x"], ["model", "anthropic/claude-x"]]
    monkeypatch.setenv("EMPORIA_DECLARE_MODEL", "off")
    assert identity_card.model_tags() == [] and identity_card.with_model_tag([["t", "x"]]) == [["t", "x"]]


def test_the_model_tag_is_a_valid_post_tag():
    assert not wire.tags_problem([["model", "nous/hermes-4"]])


# ── the CLI, the plugin hook, the SDK, the setup block ───────────────────────

def _load_plugin(name):
    import importlib.util
    root = Path(__file__).resolve().parents[1] / "hermes_plugin" / "emporia"
    spec = importlib.util.spec_from_file_location(f"emporia_{name}", root / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_plugin_hook_records_the_turns_model_and_off_records_nothing(tmp_path, monkeypatch):
    hook = _load_plugin("model_declare")
    monkeypatch.setattr(hook, "_home", lambda: tmp_path)
    monkeypatch.setattr(hook, "_setting", lambda name: "")
    hook.pre_llm_call(platform="telegram", model="anthropic/claude-x", session_id="s")
    assert identity_card.current_model(tmp_path) == "anthropic/claude-x"
    hook.pre_llm_call(model="a model with spaces")                       # not a name: ignored
    hook.pre_llm_call(model="")
    assert identity_card.current_model(tmp_path) == "anthropic/claude-x"
    (tmp_path / "plugin-data" / "emporia" / "model.json").unlink()
    monkeypatch.setattr(hook, "_setting", lambda name: "off")
    hook.pre_llm_call(model="anthropic/claude-x")
    assert not (tmp_path / "plugin-data" / "emporia" / "model.json").exists()


def test_the_plugin_registers_the_model_hook():
    source = (Path(__file__).resolve().parents[1] / "hermes_plugin" / "emporia" / "__init__.py").read_text()
    assert 'register_hook("pre_llm_call", model_declare.pre_llm_call)' in source


def test_setup_assigns_a_label_once_and_the_next_agent_reuses_it(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    a, b = root / "profiles" / "a", root / "profiles" / "b"
    for p in (a, b):
        p.mkdir(parents=True)
        (p / "config.yaml").write_text("{}\n")
    label, how = identity_card.assign_operator(a)
    assert how == "minted" and operators.valid_label(label)
    assert identity_card.assign_operator(a) == (label, "kept")
    assert identity_card.assign_operator(b) == (label, "adopted")           # the same person's next agent
    other = operators.new_label()
    assert identity_card.assign_operator(b, same_as=other) == (other, "adopted")
    assert identity_card.label_of_profile(b) == other and identity_card.label_of_profile(a) == label
    with pytest.raises(ValueError):
        identity_card.assign_operator(b, same_as="op_short")


def test_the_cli_shows_and_adopts_an_operator_label(tmp_path, monkeypatch, capsys):
    import yaml

    from emporia import cli
    root = tmp_path / "profiles"
    prof = root / "shop"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text(yaml.dump({"model": {"default": "m"}}))
    (prof / ".env").write_text("EMPORIA_AGENT_ID=shop\n")
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    monkeypatch.setattr(cli, "this_profile", lambda: prof)
    assert cli.main(["operator", "label", "--profile", "shop"]) == 1            # none yet: says how
    assert "--same-as" in capsys.readouterr().out
    mine = operators.new_label()
    assert cli.main(["operator", "label", "--same-as", mine, "--profile", "shop"]) == 0
    assert "adopted" in capsys.readouterr().out
    assert cli.main(["operator", "label", "--profile", "shop"]) == 0
    out = capsys.readouterr().out
    assert mine in out and "unproven" in out and "merges" in out
    assert cli.main(["operator", "label", "--same-as", "nope", "--profile", "shop"]) == 1


def test_the_setup_block_discloses_the_declared_model_and_how_to_turn_it_off():
    from emporia import cli
    text = "\n".join(cli.welcome_lines("shop", "shop", "https://relay.example", operators.new_label()))
    assert "EMPORIA_DECLARE_MODEL=off" in text and "never an endpoint or a key" in text
    assert "declared" in text and "op_" in text


def test_the_sdk_takes_a_label_makes_one_and_refuses_a_malformed_one(tmp_path, monkeypatch):
    from emporia import identity
    from emporia.agent_sdk import EmporiaAgent
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.delenv("EMPORIA_OPERATOR_LABEL", raising=False)
    given = operators.new_label()
    assert EmporiaAgent("http://r.invalid", "sdk_a", "ab" * 32, operator_label=given).operator_label() == given
    made = EmporiaAgent("http://r.invalid", "sdk_b", "cd" * 32).operator_label()
    assert operators.valid_label(made)
    assert EmporiaAgent("http://r.invalid", "sdk_b", "cd" * 32).operator_label() == made     # kept beside its key
    with pytest.raises(ValueError):
        EmporiaAgent("http://r.invalid", "sdk_c", "ef" * 32, operator_label="op_bad")


def test_the_signing_door_names_the_signer_by_stable_id(tmp_path, monkeypatch):
    from emporia import identity, signing
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    pub = identity.get_public_key_hex("door_agent")
    stable = wire.stable_id_for(pub)
    sid = "sess_door"
    p, _ = signing.signed("door_agent", "session-join", sid, "door_agent", "2026-11-07T00:00:00+00:00")
    assert p["stable_id"] == stable and "agent_id" not in p
    p, _ = signing.signed("door_agent", "room-invite", "room_1", "guest", "2026-11-07T00:00:00+00:00")
    assert p["stable_id"] == stable and p["invitee_id"] == "guest"
    p, _ = signing.signed("door_agent", "move", "door_agent", "move", {"uci": "e2e4"}, "opening the game", sid, 0,
                          model_commit=model_commit.commit("m", "salt-salt-1"))
    assert p["stable_id"] == stable and p["model_commit"]
    p, _ = signing.signed("door_agent", "removal", "post", "pst_1", "door_agent", "2026-11-07T00:00:00+00:00")
    assert p["stable_id"] == stable and p["type"] == "emporia:delete:v3"
    monkeypatch.setattr(signing, "stable_resolver", lambda aid: "gm_rotated")           # a rotated key: the relay's id
    signing._STABLE.clear()
    p, _ = signing.signed("door_agent", "session-leave", sid, "door_agent", "2026-11-07T00:00:00+00:00")
    assert p["stable_id"] == "gm_rotated"
    signing._STABLE.clear()


def test_setup_agent_gives_the_first_agent_a_label_and_the_next_one_the_same(profiles, capsys):  # noqa: F811
    import os
    from unittest.mock import patch

    from emporia import cli
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "first", "--relay-url", "http://127.0.0.1:9", "--keys", "files"]) == 0
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "second", "--relay-url", "http://127.0.0.1:9", "--keys", "files"]) == 0
    label = identity_card.label_of_profile(root / "first")
    assert operators.valid_label(label)
    assert identity_card.label_of_profile(root / "second") == label, "one person's agents share their label"
    capsys.readouterr()
    assert cli.main(["welcome", "second"]) == 0
    out = capsys.readouterr().out
    assert label in out and "EMPORIA_DECLARE_MODEL=off" in out
