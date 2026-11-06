"""Signer phase 2a (P293): every signature an agent makes goes through one door, with a rule per kind."""
import ast
import inspect
from pathlib import Path

import pytest

from emporia import identity, signing, wire

SRC = Path(signing.__file__).parent
PERSON_SIGNED = ("signed_key_recovery_payload", "signed_bug_verdict_payload", "signed_idea_verdict_payload")


def test_no_verdict_is_an_agent_kind():
    """Security review pass 2 (P1): a verdict awards points, so it is never signed for an agent."""
    built = {k.builder for k in signing.KINDS.values()}
    assert not {"signed_bug_verdict_payload", "signed_idea_verdict_payload"} & built
    assert "idea-verdict" not in signing.KINDS and "bug-verdict" not in signing.KINDS
    with pytest.raises(signing.Refused, match="no rule"):
        signing.sign("p293-agent", "idea-verdict", "pst_1", "adopted", "abc1234", "", "e2e", "k", "t")


def ctx(**env):
    return signing.Context("p293-agent", env=env, own_wallets=lambda aid: {"0xOWN", "SoLOwn"})


def test_every_wire_builder_an_agent_signs_has_a_rule():
    built = {k.builder for k in signing.KINDS.values() if k.builder}
    agent_signed = {n for n, _ in inspect.getmembers(wire, inspect.isfunction)
                    if n.startswith("signed_") and n not in PERSON_SIGNED}
    # what the agent signs is in the table; the recovery step is signed by the person's offline key,
    # and the verdicts by the operator's verdict key (security review pass 2, P1)
    assert agent_signed - built == set(), sorted(agent_signed - built)


def test_an_unknown_kind_is_refused_never_signed():
    with pytest.raises(signing.Refused, match="no rule"):
        signing.check("anything-new", {}, ctx())
    with pytest.raises(signing.Refused, match="no rule"):
        signing.payload("anything-new")


@pytest.mark.parametrize("kind", ["key-rotation", "recovery-commitment"])
def test_who_the_agent_is_stays_its_persons(kind):
    with pytest.raises(signing.Refused, match="person"):
        signing.check(kind, {}, ctx())


def test_routine_kinds_sign_at_once_with_no_one_asked():
    for kind in ("move", "room-message", "dm", "agora-post", "listing", "session-join", "room-leave", "removal"):
        signing.check(kind, {}, ctx())


def test_a_payout_goes_only_to_its_own_wallets_or_its_persons_list():
    signing.check("payout-wallet", {"address": "0xOWN"}, ctx())
    signing.check("payout-wallet", {"address": "0xown"}, ctx())               # EVM case does not matter
    with pytest.raises(signing.Refused, match="not one of this agent's own wallets"):
        signing.check("payout-wallet", {"address": "0xTHIEF"}, ctx())
    signing.check("payout-wallet", {"address": "0xSAFE"}, ctx(EMPORIA_PAYOUT_ADDRESSES="0xSAFE, 0xOTHER"))


def test_a_home_and_backups_only_where_its_person_said():
    home = {"EMPORIA_RELAY_URL": "https://home.example"}
    signing.check("relay-list", {"home": "https://home.example/", "backups": []}, ctx(**home))
    with pytest.raises(signing.Refused, match="its person's choice"):
        signing.check("relay-list", {"home": "https://evil.example", "backups": []}, ctx(**home))
    signing.check("relay-list", {"home": "https://new.example", "backups": []},
                  ctx(**home, EMPORIA_VISIT_RELAYS="https://new.example"))
    with pytest.raises(signing.Refused, match="backup"):
        signing.check("relay-list", {"home": "https://home.example", "backups": ["https://evil.example"]},
                      ctx(**home))
    signing.check("relay-list", {"home": "https://home.example", "backups": ["https://b.example"]},
                  ctx(**home, EMPORIA_BACKUP_RELAYS="https://b.example"))


def test_limits_count_only_with_a_history_and_say_when_to_retry():
    signing.check("removal", {}, ctx())                                      # in-process: no rate counted
    h = signing.History()
    for _ in range(10):
        signing.check("removal", {}, ctx(), history=h, now=1000.0)
    with pytest.raises(signing.Limited) as e:
        signing.check("removal", {}, ctx(), history=h, now=1000.0)
    assert "10 per day" in str(e.value) and e.value.retry_after == pytest.approx(86400)
    signing.check("removal", {}, ctx(), history=h, now=1000.0 + 86401)       # a day later
    h2 = signing.History()
    signing.check("removal", {}, ctx(EMPORIA_SIGN_LIMITS="removal=1/h"), history=h2, now=0)
    with pytest.raises(signing.Limited, match="1 per h"):
        signing.check("removal", {}, ctx(EMPORIA_SIGN_LIMITS="removal=1/h"), history=h2, now=10)


def test_a_malformed_limit_is_refused_loudly():
    for bad in ("removal=ten/day", "nosuch=1/h", "removal=1/week"):
        with pytest.raises(ValueError, match="EMPORIA_SIGN_LIMITS"):
            signing.limits({"EMPORIA_SIGN_LIMITS": bad})


def test_the_same_bytes_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    args = ("room-1", "hello", "chat", "n" * 32)
    p, sig = signing.signed("p293-signer", "room-message", *args)
    assert p == wire.signed_room_message_payload(*args)
    assert sig == identity.sign(p, "p293-signer")
    assert identity.verify(p, sig, identity.generate_or_load_keypair("p293-signer")[1].hex())
    nonce = "ab" * 16
    pub = identity.generate_or_load_keypair("p293-signer")[1].hex()
    sig_in = signing.sign_in("p293-signer", nonce, "https://relay.example", pub)
    assert identity.verify(wire.signed_registration_payload(nonce, "https://relay.example", "p293-signer", pub),
                           sig_in, pub)


def _calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "emporia.identity":
            out += [f"import {a.name}" for a in node.names if a.name in ("sign", "sign_raw")]
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "identity" \
                and node.attr in ("sign", "sign_raw"):
            out.append(f"identity.{node.attr}")
    return out


@pytest.mark.parametrize("module", ["mcp_server.py", "agent_sdk.py"])
def test_the_agent_signs_only_through_signing(module):
    assert _calls(SRC / module) == [], f"{module} signs around signing.py"


def test_every_id_emporia_derives_is_sha256(tmp_path):
    """P294: agent ids, recovery commitments and listing ids are SHA-256 with v2 labels — one hash for
    everything Emporia chooses — and a relay holding agents from before refuses to start."""
    import hashlib

    key = "ab" * 32
    assert wire.stable_id_for(key) == hashlib.sha256(b"emporia:agent:v2:" + bytes.fromhex(key)).hexdigest()
    assert wire.recovery_commitment_for(key) == hashlib.sha256(
        b"emporia:recovery:v2:" + bytes.fromhex(key)).hexdigest()
    assert wire.listing_id_for("sid", "n1") == "lst_" + hashlib.sha256(b"emporia:listing:v2:sid:n1").hexdigest()[:32]
    for name in ("wire.py", "identity.py", "relay_server.py"):
        assert "sha3_256(" not in (SRC / name).read_text(), name
