"""Where keys are read from does not depend on import order, and looking one
up does not create one (P103)."""
import ast
from pathlib import Path

from emporia import identity

SRC = Path(__file__).resolve().parent.parent / "src" / "emporia" / "identity.py"


def test_the_profile_settings_load_before_the_key_dir_is_decided():
    """KEY_DIR is fixed at import. If identity is imported before the profile
    .env is merged, it reads ~/.hermes/keys -- so identity merges it first."""
    tree = ast.parse(SRC.read_text())
    config_import = key_dir = None
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "emporia" and any(
                a.name == "config" for a in node.names):
            config_import = node.lineno
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "KEY_DIR" for t in node.targets):
            key_dir = node.lineno
    assert config_import and key_dir and config_import < key_dir


def test_reading_a_key_never_creates_one():
    assert identity.read_public_key_hex("p103_nobody_here") is None
    assert not (identity.KEY_DIR / "p103_nobody_here.priv").exists()


def test_reading_returns_the_same_key_the_generator_made():
    made = identity.get_public_key_hex("p103_somebody")
    assert identity.read_public_key_hex("p103_somebody") == made


def test_distinct_agent_ids_never_share_a_key_or_wallet_file(tmp_path):
    """P113: `my-agent` and `my_agent` are two identities on the relay; the
    key file name used to fold `-` into `_`, giving them one key and one x402
    wallet in a shared key directory."""
    from unittest.mock import patch

    from emporia import agent_wallet, identity

    with patch.object(identity, "KEY_DIR", tmp_path):
        assert identity._key_path("my-agent") != identity._key_path("my_agent")
        assert identity._enc_key_path("my-agent") != identity._enc_key_path("my_agent")
        assert agent_wallet.wallet_path("my-agent") != agent_wallet.wallet_path("my_agent")
        assert identity._key_path("alpha").name == "alpha.priv"
        assert identity._key_path("Hackathon_Hermes").name == "hackathon_hermes.priv"
