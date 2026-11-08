"""The plugin's copy of the claim check (hermes_plugin/emporia/x_proof.py) agrees with the runtime's, byte for byte."""
import importlib.util
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519

from emporia import wire, x_link

PLUGIN = Path(__file__).resolve().parents[1] / "hermes_plugin" / "emporia" / "x_proof.py"
spec = importlib.util.spec_from_file_location("emporia_x_proof_test", PLUGIN)
xp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(xp)


def test_the_plugin_copy_imports_nothing_from_emporia():
    src = PLUGIN.read_text()
    assert "from emporia" not in src.replace("emporia/", "") and "import emporia" not in src


def test_its_ed25519_agrees_with_the_library_on_good_and_bad_signatures():
    for i in range(3):
        priv = ed25519.Ed25519PrivateKey.generate()
        pub = priv.public_key().public_bytes_raw()
        msg = os.urandom(40 + i)
        sig = priv.sign(msg)
        assert xp.ed25519_verify(pub, msg, sig)
        assert not xp.ed25519_verify(pub, msg + b"x", sig)
        assert not xp.ed25519_verify(pub, msg, sig[:-1] + bytes([sig[-1] ^ 1]))
        assert not xp.ed25519_verify(os.urandom(32), msg, sig)
    assert not xp.ed25519_verify(b"\0" * 31, b"m", b"\0" * 64) and not xp.ed25519_verify(b"\0" * 32, b"m", b"\0" * 63)


def test_it_reads_the_runtimes_claim_lines_and_reproduces_the_signed_bytes():
    name = "xproof_agent"
    made = x_link.make_claim(name, "Kestrel_X", "http://relay.example:8088", now=1_800_000_000)
    parsed = x_link.parse_line("hello\n" + made["line"])
    mine = xp.parse_last_line("hello\n" + made["line"])
    assert mine["token"] == parsed["token"] and mine["epoch"] == parsed["issued_epoch"] and mine["name"] == name
    stable = made["stable_id"]
    runtime_bytes = json.dumps(x_link.claim_payload(parsed, stable, "kestrel_x"), sort_keys=True,
                               separators=(",", ":")).encode()
    assert xp.claim_bytes(stable, made["address"], "kestrel_x", made["relay_host"], mine["epoch"]) == runtime_bytes
    assert xp.ed25519_verify(bytes.fromhex(made["public_key"]), runtime_bytes, mine["sig"])
    assert xp.x_length("a▮b→") == x_link.x_length("a▮b→") == 6


def test_the_line_shape_matches_the_runtimes():
    assert xp.SEP == x_link.SEP and xp.TOKEN_LEN == x_link.TOKEN_LEN and xp.MAX_POST == x_link.MAX_POST
    assert xp.CLAIM_TYPE == wire.X_CLAIM_TYPE
    assert (xp.CLAIMS_FILE, xp.PROOFS_FILE) == (x_link.STATE_FILE, x_link.PROOFS_FILE)
    assert xp._LINE.pattern == x_link._LINE.pattern


def test_only_the_last_line_counts():
    made = x_link.make_claim("xproof_agent", "kestrel_x", "http://relay.example")
    assert xp.parse_last_line(made["line"] + "\nmore") is None
    assert xp.parse_last_line("a\n" + made["line"] + "\n\n") is not None
    assert xp.parse_last_line("not " + made["line"]) is None            # whole line, nothing before it
    assert xp.parse_last_line("") is None
