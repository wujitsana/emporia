"""Security review pass 2 (2026-11-06): each test fails without its fix."""
import base64

from emporia import signer
from tests.test_signer_backends import setup  # noqa: F401  (fixture)
from tests.test_signer_phase2 import AGENT, _open, _sealed, held  # noqa: F401  (fixture)


def test_the_signer_takes_its_agent_id_in_one_spelling_only(held):  # noqa: F811
    """A token is the agent's, under the id the signer files it as. Before, `P2_AGENT` found
    p2_agent's token (the folder folds case) while the open cap counted per spelling, so a
    new spelling opened past the cap and never reached the burst alert."""
    signer.save_policy(AGENT, {"EMPORIA_RELAY_URL": "https://home.example", "EMPORIA_SIGNER_OPEN_PER_MIN": "1"})
    env, aad = _sealed("one", held.made["encryption"])
    assert _open(held, env, AGENT, aad).status_code == 200
    assert _open(held, env, AGENT, aad).status_code == 429                 # the cap
    for spelling in (AGENT.upper(), f" {AGENT}", f"{AGENT}!"):
        r = held.app.post("/v1/open", json={"envelope": env, "label": AGENT,
                                            "aad": base64.b64encode(aad).decode()},
                          headers={"authorization": f"Bearer {held.token}", "x-emporia-agent": spelling})
        assert r.status_code == 401, spelling
        assert "one" not in r.text
    r = held.app.get("/v1/wallet", headers={"authorization": f"Bearer {held.token}",
                                            "x-emporia-agent": AGENT.upper()})
    assert r.status_code == 401


def test_the_key_guard_keeps_mainnet_and_limits_out_of_the_agents_own_config():
    """`emporia wallet mainnet` is person-only and `wallet limits` waits for the person's yes,
    but the settings themselves went straight into the agent's config with no word from the
    person: `hermes config set EMPORIA_X402_MAINNET 1`, a line added to its .env, a key in
    config.yaml."""
    from hermes_plugin.emporia import key_guard as g

    def action(tool, **args):
        return (g.pre_tool_call(tool, args) or {}).get("action")

    for cmd in ("hermes config set EMPORIA_X402_MAINNET 1",
                "echo EMPORIA_X402_MAINNET=1 >> ~/.hermes/profiles/a/.env"):
        assert action("terminal", command=cmd) == "block", cmd
    assert action("write_file", path="/h/profiles/a/.env", content="EMPORIA_X402_MAINNET=true\n") == "block"
    assert action("write_file", path="/h/profiles/a/config.yaml",
                  content="mcp_servers:\n  emporia:\n    env:\n      EMPORIA_X402_MAINNET: '1'\n") == "block"
    for cmd in ("hermes config set EMPORIA_X402_MAX_DAILY_CENTS 100000",
                "hermes config set EMPORIA_X402_PAY_NETWORKS eip155:8453",
                "echo EMPORIA_PAYMENT_APPROVAL_CENTS= >> ~/.hermes/.env"):
        assert action("terminal", command=cmd) == "approve", cmd
    # Emporia's own source and tests still name these freely; so does reading them.
    assert action("write_file", path="/repo/src/emporia/x.py", content='env = {"EMPORIA_X402_MAINNET": "0"}') is None
    assert action("terminal", command="grep -rn EMPORIA_X402_MAINNET src/") is None


def test_the_key_guard_keeps_the_mm_session_and_the_signers_files_by_name():
    """`~/.metamask` was matched only at the end of a command or before a `/`, so `tar ... ~/.metamask
    && ...` or `cp -r ~/.metamask /x` copied the signer user's session; `mm` through npx signed;
    and a signer folder set by EMPORIA_SIGNER_DIR (as the agents' user) had its admin token,
    back-end secrets and keys readable by path."""
    from hermes_plugin.emporia import key_guard as g
    for cmd in ("tar czf /x/m.tgz ~/.metamask && echo ok", "cp -r ~/.metamask /x", "zip -r m ~/.metamask;",
                "npx @metamask/agent-wallet wallet sign-typed-data --payload {}",
                "cat /srv/signer/admin.token", "cat /srv/signer/backends/kms.secret",
                "xxd /srv/signer/agents/a/keys/identity.ed25519"):
        assert (g.pre_tool_call("terminal", {"command": cmd}) or {}).get("action") == "block", cmd
    for fine in ("open https://docs.metamask.io/agent-wallet", "npm view @metamask/agent-wallet version"):
        assert g.pre_tool_call("terminal", {"command": fine}) is None, fine


def test_a_payment_its_back_end_refused_does_not_count_against_the_day(setup, monkeypatch):  # noqa: F811
    """The day's total was charged before the back end was asked, and never given back when it
    said no (a provider's policy, MetaMask's Guard Mode or its 2FA timing out, a signature from
    the wrong key): each refusal used up the daily cap, until the agent could pay nothing."""
    import pytest

    from emporia import agent_wallet
    from tests.test_signer_backends import AGENT as BA
    from tests.test_signer_backends import _challenge_for, _privy
    _privy(setup, monkeypatch, max_value=300 * 10_000)
    signer.make_agent_keys(BA, {"evm": "privy1"})
    assert agent_wallet.sign_exact(BA, _challenge_for(250))
    assert agent_wallet.spent_today_atomic(BA) == 250 * 10_000
    for _ in range(3):
        with pytest.raises(agent_wallet.AgentWalletError, match="Policy violation"):
            agent_wallet.sign_exact(BA, _challenge_for(400))          # under our cap, over Privy's
    assert agent_wallet.spent_today_atomic(BA) == 250 * 10_000, "a refused payment is not a payment"
