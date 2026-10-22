"""P114: a Nous token that is current when it is used, from Hermes when the
configured one has expired."""
import base64
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from emporia import nous_token


def _jwt(exp: float) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64({'exp': int(exp)})}.sig"


def test_a_current_configured_token_is_used_without_asking_hermes():
    fresh = _jwt(time.time() + 3600)
    with patch.object(nous_token, "from_hermes") as hermes:
        assert nous_token.current(fresh) == fresh
    hermes.assert_not_called()


def test_an_expired_token_is_replaced_by_hermess_current_one(monkeypatch):
    expired, renewed = _jwt(time.time() - 60), _jwt(time.time() + 3600)
    monkeypatch.setenv("EMPORIA_DOTENV", "/profiles/alpha/.env")
    with patch.object(nous_token, "from_hermes", return_value=renewed) as hermes:
        assert nous_token.current(expired) == renewed
    hermes.assert_called_once_with(Path("/profiles/alpha"))


def test_without_hermes_the_configured_token_stands():
    expired = _jwt(time.time() - 60)
    with patch.object(nous_token, "from_hermes", return_value=None):
        assert nous_token.current(expired) == expired
        assert nous_token.current("") == ""


def test_from_hermes_runs_hermess_resolver_for_this_profile(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("")
    (root / ".venv" / "lib" / "python3.14" / "site-packages").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "bin" / "hermes").write_text("")
    monkeypatch.setenv("EMPORIA_HERMES_BIN", str(root / "bin" / "hermes"))
    token = _jwt(time.time() + 3600)
    seen = {}

    def run(cmd, env, **kw):
        seen.update(cmd=cmd, env=env)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"some log line\n{token}\n", stderr="")
    with patch("subprocess.run", side_effect=run):
        assert nous_token.from_hermes(Path("/profiles/beta")) == token
    assert seen["env"]["HERMES_HOME"] == "/profiles/beta"
    assert "resolve_nous_access_token" in seen["cmd"][-1]

    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "revoked")):
        assert nous_token.from_hermes(Path("/profiles/alpha")) is None
