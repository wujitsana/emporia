"""Catalog rules 11 and 13: an agent sends its Nous access token to the relay only when the
person turned Nous verification on (EMPORIA_NOUS_VERIFY). Off by default: key_only, and no
token leaves the machine."""
import time
from unittest.mock import patch

import pytest

from emporia import nous_token
from tests.test_nous_token import _jwt


def test_verification_is_off_unless_turned_on(monkeypatch):
    monkeypatch.delenv("EMPORIA_NOUS_VERIFY", raising=False)
    assert nous_token.verify_enabled() is False
    for on in ("1", "true", "YES", " on "):
        monkeypatch.setenv("EMPORIA_NOUS_VERIFY", on)
        assert nous_token.verify_enabled() is True
    for off in ("0", "false", "", "no"):
        monkeypatch.setenv("EMPORIA_NOUS_VERIFY", off)
        assert nous_token.verify_enabled() is False
    assert nous_token.verify_enabled({"EMPORIA_NOUS_VERIFY": "1"}) is True     # a profile's .env


def test_off_means_no_token_and_hermes_is_not_even_asked(monkeypatch):
    monkeypatch.delenv("EMPORIA_NOUS_VERIFY", raising=False)
    configured = _jwt(time.time() + 3600)
    with patch.object(nous_token, "from_hermes") as hermes:
        assert nous_token.for_registration("", configured) == ""
    hermes.assert_not_called()
    assert nous_token.for_registration("passed-on-purpose", configured) == "passed-on-purpose"


def test_on_means_a_current_token(monkeypatch):
    monkeypatch.setenv("EMPORIA_NOUS_VERIFY", "1")
    fresh = _jwt(time.time() + 3600)
    with patch.object(nous_token, "from_hermes", return_value=fresh):
        assert nous_token.for_registration("", _jwt(time.time() - 60)) == fresh


def test_login_turns_verification_on_for_the_profile_it_signs_in(tmp_path, monkeypatch, capsys):
    """`emporia login` is the person asking for nous_verified: it turns the setting on in that
    profile's .env and says the relay will see the token."""
    import argparse

    from emporia import cli
    profile = tmp_path / "profiles" / "zed"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: {}\n")
    (profile / ".env").write_text("EMPORIA_AGENT_ID=zed\n")
    monkeypatch.setattr(cli, "profile_dir", lambda name: profile)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(nous_token, "from_hermes", lambda p=None: _jwt(time.time() + 3600))
    assert cli.cmd_login(argparse.Namespace(profile="zed")) == 0
    lines = (profile / ".env").read_text().splitlines()
    assert "EMPORIA_NOUS_VERIFY=1" in lines and "EMPORIA_AGENT_ID=zed" in lines
    assert "sent a current Nous access token" in capsys.readouterr().out
    assert oct((profile / ".env").stat().st_mode)[-3:] == "600"


def test_login_signs_in_the_profile_hermes_ran_it_for(tmp_path, monkeypatch):
    """Release review (11-06): `hermes -p shop emporia login` signed in the profile holding the
    runtime (a profile setup made shares the runtime of the one that made it), and
    `hermes emporia login --profile shop` cannot say otherwise: Hermes takes `--profile` as its
    own switch. The plugin hands the runtime the calling profile's HERMES_HOME; login uses it."""
    import argparse

    from emporia import cli
    root = tmp_path / "root"
    for name in ("maker", "shop"):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / "config.yaml").write_text("model: {}\n")
    monkeypatch.setattr(cli, "this_profile", lambda: root / "profiles" / "maker")
    monkeypatch.setattr(cli, "hermes_root", lambda: root)
    signed = []
    monkeypatch.setattr(cli, "profile_dir", lambda name: root / "profiles" / name)
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: signed.append(argv[2]))
    monkeypatch.setattr(nous_token, "from_hermes", lambda p=None: "")
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "shop"))
    cli.cmd_login(argparse.Namespace(profile=""))
    monkeypatch.setenv("HERMES_HOME", str(root))                  # the root: no profile named
    cli.cmd_login(argparse.Namespace(profile=""))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere" / "profiles" / "shop"))
    cli.cmd_login(argparse.Namespace(profile=""))                  # another root's: not ours
    cli.cmd_login(argparse.Namespace(profile="shop"))              # a name given still wins
    assert signed == ["shop", "maker", "maker", "shop"], signed


@pytest.mark.parametrize("verify,sent", [("", False), ("1", True)])
def test_register_agent_carries_the_token_only_when_verification_is_on(monkeypatch, verify, sent):
    from emporia import mcp_server, wire
    monkeypatch.setenv("EMPORIA_NOUS_VERIFY", verify)
    fresh = _jwt(time.time() + 3600)
    monkeypatch.setattr(nous_token, "from_hermes", lambda p=None: fresh)
    bodies = []

    class R:
        status_code = 200

        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

        def raise_for_status(self):
            return None

    def post(url, json=None, timeout=None):
        if url.endswith("/agents/challenge"):
            return R({"nonce": "n", "challenge_id": "c"})
        bodies.append(json)
        return R({"agent_id": "zed", "trust_level": "nous_verified" if sent else "key_only"})

    monkeypatch.setattr(mcp_server.httpx, "post", post)
    monkeypatch.setattr(mcp_server.signing, "sign_in", lambda a, n: "00")
    monkeypatch.setattr(mcp_server.signing, "sign", lambda *a: "00")
    monkeypatch.setattr("emporia.identity.get_public_key_hex", lambda a: "11" * 32)
    monkeypatch.setattr("emporia.identity.get_encryption_public_key_hex", lambda a: "22" * 32)
    for name in ("_publish_relay_list", "_keep_own_record", "_archive_in_background"):
        monkeypatch.setattr(mcp_server, name, lambda *a, **k: None)
    assert wire.registration_body  # the body builder the tool uses
    out = mcp_server.register_agent(agent_id="zed")
    assert out["success"], out
    claims = bodies[0].get("identity_claims")
    assert (claims == [{"provider": "nous", "token": fresh}]) if sent else not claims
