"""P275 (l): manual-testing.md "Signer (P203)", scripted. A real signer process on a
loopback port, the person's CLI steps, and the agent paying from a process of its
own (its own .env, the way its MCP server runs). Practice mode — the same OS user —
so this proves the mechanics, not the separation ("Signer apart" stays a person's).
Testnet x402 payments are signed; nothing is sent to any chain."""
import json
import os
import re
import signal
import time
from pathlib import Path

from emporia import cli
from emporia.key_source import unset_env_lines
from conftest import payee_for
from tests.live.harness import free_port
from tests.test_cli import profiles  # noqa: F401  (fixture)

#: Taken before conftest's _no_real_signer swaps it out: this test does start one, in its own
#: temporary folder, and stops it (by the pid it recorded) whatever happens.
_START_SIGNER = cli._signer_daemon
PAYEE = payee_for("the_payee", evm="0x" + "11" * 20)
PAY = ("import json, os\nfrom emporia import agent_wallet as w\n"
       "try:\n"
       "    p = w.sign_exact('alpha', json.loads(os.environ['CHALLENGE']))\n"
       "    print(json.dumps({'ok': bool(p), 'address': w.address('alpha'),"
       " 'signer': bool(os.getenv('EMPORIA_SIGNER_URL'))}))\n"
       "except Exception as e:\n"
       "    print(json.dumps({'ok': False, 'error': str(e)}))\n")


def _challenge(cents: int) -> dict:
    return {"x402": {"x402Version": 2, "resource": {"url": "http://relay/game"},
                     "accepts": [{"scheme": "exact", "network": "eip155:84532", "amount": str(cents * 10_000),
                                  "payTo": "0x" + "11" * 20, "maxTimeoutSeconds": 60, "asset": "0x" + "22" * 20,
                                  "extra": {"name": "USDC", "version": "2"}}]},
            "payee": PAYEE}


def _env_of(profile: Path) -> dict:
    return dict(line.split("=", 1) for line in (profile / ".env").read_text().splitlines()
                if "=" in line and not line.startswith("#"))


def _gone(pid: int) -> bool:
    for _ in range(40):
        try:
            state = Path(f"/proc/{pid}/status").read_text().split("State:")[1].split()[0]
        except (OSError, IndexError):
            return True
        if state in ("Z", "X"):          # exited; a zombie until this process reaps it
            return True
        time.sleep(0.25)
    return False


def test_a_wallet_moves_behind_the_signer_pays_waits_for_approval_and_comes_back(
        profiles, tmp_path, monkeypatch, capsys):  # noqa: F811
    root, _ = profiles
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    for k in [k for k in os.environ if k.startswith(("HERMES_SESSION_", "EMPORIA_SIGNER_"))]:
        monkeypatch.delenv(k)
    monkeypatch.setattr(cli, "_signer_daemon", _START_SIGNER)
    url = f"http://127.0.0.1:{free_port()}"
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "sig"))
    monkeypatch.setenv("EMPORIA_SIGNER_SOCKET", str(tmp_path / "unused.sock"))   # never the person's own signer

    def run(*argv: str) -> tuple[int, str]:
        capsys.readouterr()
        rc = cli.main(list(argv))
        return rc, capsys.readouterr().out

    def pay(cents: int) -> dict:
        r = cli._as_agent(root / "alpha", PAY, CHALLENGE=json.dumps(_challenge(cents)))
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"ok": False, "error": (r.stdout + r.stderr)[-600:]}

    assert run("setup", "agent", "alpha", "--keys", "files", "--wallet", "--max-daily-cents", "700")[0] == 0
    monkeypatch.setattr(cli, "_TTY", True)                 # from here, the person's own terminal
    prof, key_dir = root / "alpha", root / "alpha" / "home" / ".hermes" / "keys"
    first = pay(50)
    assert first["ok"] and not first["signer"], first
    address = first["address"]

    # Real money: refused here, offering the signer (the person says no).
    asked = []
    monkeypatch.setattr(cli, "ask", lambda q, default=True, **k: asked.append(q) or False)
    rc, out = run("wallet", "mainnet", "alpha")
    assert rc != 0 and "signer add alpha" in out and "behind the signer" in asked[0]
    monkeypatch.setattr(cli, "ask", lambda q, default=True, **k: True)

    rc, out = run("signer", "start", "--port", url.rsplit(":", 1)[1], "--url", url)
    assert rc == 0 and "practice mode" in out, out
    pid = int((tmp_path / "sig" / "signer.pid").read_text())
    try:
        rc, out = run("signer", "status", "--url", url)
        assert rc == 0 and "PRACTICE MODE" in out, out

        keys = sorted(p.name for p in key_dir.glob("alpha.x402.*"))
        rc, out = run("signer", "add", "alpha", "--url", url)
        env = _env_of(prof)
        assert rc == 0 and env["EMPORIA_SIGNER_URL"] == url and env["EMPORIA_SIGNER_TOKEN"] not in out, out
        assert keys and not list(key_dir.glob("alpha.x402.*"))
        assert not [p for p in root.rglob("*.x402.*") if "spend" not in p.name], "no wallet key left in a profile"

        through = pay(50)
        assert through["ok"] and through["signer"] and through["address"] == address, through

        assert run("signer", "policy", "alpha", "set", "EMPORIA_PAYMENT_APPROVAL_CENTS", "0")[0] == 0
        held = pay(50)
        intent = re.search(r"int_[A-Za-z0-9_-]+", held.get("error", ""))
        assert not held["ok"] and intent, held
        intent = intent.group(0)
        rc, out = run("signer", "pending", "--url", url)
        assert intent in out
        monkeypatch.setenv("HERMES_SESSION_ID", "x")       # an agent's terminal tool
        rc, out = run("signer", "approve", intent, "--url", url)
        assert rc != 0 and "only a person" in out
        monkeypatch.delenv("HERMES_SESSION_ID")
        rc, out = run("signer", "approve", intent, "--url", url)
        assert rc == 0 and "approved" in out, out
        assert pay(50)["ok"], "the retry is paid"
        again = pay(50)
        assert not again["ok"] and "int_" in again["error"], "an approval pays once"
        rc, out = run("signer", "deny", re.search(r"int_[A-Za-z0-9_-]+", again["error"]).group(0), "--url", url)
        assert rc == 0 and "denied" in out

        rc, out = run("signer", "remove", "alpha")
        env = _env_of(prof)
        assert rc == 0 and "EMPORIA_SIGNER_URL" not in env, out
        assert set(keys) <= {p.name for p in key_dir.glob("alpha.x402.*")}
        assert env["EMPORIA_X402_MAX_DAILY_CENTS"] == "700"
        assert "APPROVAL_CENTS" in pay(50)["error"], "the threshold set at the signer came back with it"
        unset_env_lines(prof / ".env", ["EMPORIA_PAYMENT_APPROVAL_CENTS"])
        back = pay(50)
        assert back["ok"] and not back["signer"] and back["address"] == address, back
        # Signed: 4 x 50 = 200 of 700. 550 more fits only if the 100 signed behind the signer were forgotten.
        over = pay(550)
        assert not over["ok"] and "2.00 already" in over["error"], over

        assert run("signer", "stop")[0] == 0
        assert _gone(pid)
        pid = 0
    finally:
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
