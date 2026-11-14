"""The runtime's first import may be any module: with a profile .env present, config merges it at import, which
lists the setting names, which once imported identity while identity was itself importing config (the console
then failed to start for every profile with a .env; the suite never saw it because it sets EMPORIA_NO_DOTENV)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.parametrize("first", ["emporia.identity", "emporia.console.agents", "emporia.config", "emporia.mcp_server"])
def test_a_first_import_works_with_a_profile_env(tmp_path, first):
    env_file = tmp_path / ".env"
    env_file.write_text("EMPORIA_AGENT_ID=someone\nEMPORIA_RELAY_URL=http://127.0.0.1:1\n")
    env = {k: v for k, v in os.environ.items() if k != "EMPORIA_NO_DOTENV"}
    env.update(EMPORIA_DOTENV=str(env_file), PYTHONPATH=str(SRC), HERMES_HOME=str(tmp_path), HOME=str(tmp_path))
    r = subprocess.run([sys.executable, "-c", f"import {first}"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]


def test_listing_the_setting_names_does_not_import_identity():
    """The cycle itself: identity imports config, config reads a Hermes profile's .env at import through
    hermes_settings.declared_names(), which lists agent_env.key_names(). That must not import identity."""
    code = ("import sys; from emporia import agent_env; agent_env.key_names(); "
            "assert 'emporia.identity' not in sys.modules, 'key_names imports identity'")
    env = {**os.environ, "PYTHONPATH": str(SRC), "EMPORIA_NO_DOTENV": "1"}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
