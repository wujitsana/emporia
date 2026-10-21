"""The x402 escrow operator key (P96): generated only on purpose, stored 0600,
checked against its expected address, and kept off mainnet unless accepted."""
import os
import stat
from unittest.mock import patch

import pytest

pytest.importorskip("eth_account")

from emporia import identity, operator_signer as ops  # noqa: E402


@pytest.fixture
def keys(tmp_path):
    env = {k: "" for k in ("EMPORIA_OPERATOR_PRIVATE_KEY", "EMPORIA_OPERATOR_ADDRESS",
                           "EMPORIA_OPERATOR_SIGNER", "EMPORIA_ALLOW_HOT_OPERATOR_KEY")}
    with patch.object(identity, "KEY_DIR", tmp_path), patch.dict(os.environ, env):
        yield tmp_path


def test_no_key_until_someone_generates_one(keys):
    with pytest.raises(ops.OperatorSignerError, match="generate"):
        ops.load_operator_signer()
    assert not ops.key_path().exists()


def test_a_generated_key_is_owner_only_and_never_replaced(keys):
    address = ops.generate_key()
    mode = stat.S_IMODE(ops.key_path().stat().st_mode)
    assert mode == 0o600
    assert ops.load_operator_signer().address == address
    with pytest.raises(ops.OperatorSignerError, match="not replaced"):
        ops.generate_key()


def test_a_secret_manager_can_supply_the_key(keys):
    from eth_account import Account
    acct = Account.create()
    with patch.dict(os.environ, {"EMPORIA_OPERATOR_PRIVATE_KEY": acct.key.hex()}):
        assert ops.load_operator_signer().address == acct.address


def test_a_bad_key_is_refused_without_echoing_it(keys):
    with patch.dict(os.environ, {"EMPORIA_OPERATOR_PRIVATE_KEY": "0xnotakey_secretish"}):
        with pytest.raises(ops.OperatorSignerError) as e:
            ops.load_operator_signer()
    assert "secretish" not in str(e.value)


def test_the_wrong_key_is_refused(keys):
    ops.generate_key()
    with patch.dict(os.environ, {"EMPORIA_OPERATOR_ADDRESS": "0x" + "11" * 20}):
        with pytest.raises(ops.OperatorSignerError, match="wrong key"):
            ops.load_operator_signer()


def test_a_hot_key_stays_off_mainnet_unless_accepted(keys):
    ops.generate_key()
    assert ops.load_operator_signer("eip155:84532")          # Base Sepolia: fine
    with pytest.raises(ops.OperatorSignerError, match="mainnet"):
        ops.load_operator_signer("eip155:8453")
    with patch.dict(os.environ, {"EMPORIA_ALLOW_HOT_OPERATOR_KEY": "1"}):
        assert ops.load_operator_signer("eip155:8453")


def test_a_backend_that_does_not_exist_yet_fails_loudly(keys):
    ops.generate_key()
    with patch.dict(os.environ, {"EMPORIA_OPERATOR_SIGNER": "aws-kms"}):
        with pytest.raises(ops.OperatorSignerError, match="not built yet"):
            ops.load_operator_signer()
    with patch.dict(os.environ, {"EMPORIA_OPERATOR_SIGNER": "vault"}):
        with pytest.raises(ops.OperatorSignerError, match="Unknown"):
            ops.load_operator_signer()


def test_the_signer_signs(keys):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    ops.generate_key()
    signer = ops.load_operator_signer()
    message = {
        "types": {"EIP712Domain": [{"name": "name", "type": "string"}],
                  "Ping": [{"name": "n", "type": "uint256"}]},
        "domain": {"name": "emporia"}, "primaryType": "Ping", "message": {"n": 1},
    }
    sig = signer.sign_typed_data(message)
    assert Account.recover_message(encode_typed_data(full_message=message),
                                   signature=sig) == signer.address


def _git(repo, *args):
    import subprocess
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_a_key_is_never_written_where_git_would_track_it(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    with patch.object(identity, "KEY_DIR", repo / "keys"):
        with pytest.raises(ops.OperatorSignerError, match="git working tree"):
            ops.generate_key()
        assert not (repo / "keys" / ops.KEY_FILE_NAME).exists()


def test_an_ignored_key_directory_inside_a_repository_is_fine(tmp_path):
    """The usual layout: a profile home tracked in git, its key dir ignored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("keys/\n")
    with patch.object(identity, "KEY_DIR", repo / "keys"):
        assert ops.generate_key().startswith("0x")


def test_an_unverifiable_repository_is_refused(tmp_path):
    """A `.git` git cannot read is not evidence the key is ignored."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("keys/\n")
    with patch.object(identity, "KEY_DIR", repo / "keys"):
        with pytest.raises(ops.OperatorSignerError, match="git working tree"):
            ops.generate_key()
