"""The signed card's operator label, client and declared model; look-alike names; model commitments."""

import time

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
