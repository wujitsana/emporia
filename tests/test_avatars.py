"""The agent pictures: the Python generators against the shared vectors (the dashboard's TypeScript port is checked
against the same file), the registration's image, and the console's cache-only picture of a proven link."""
import base64
import hashlib
import json
import pathlib

import pytest

from emporia import avatars, chain_register
from emporia import erc8004_register as R
from tests import test_erc8004_register as _reg
from tests.test_erc8004_register import STABLE, card

target, fake = _reg.target, _reg.fake      # the registration tests' fixtures

VECTORS = json.loads((pathlib.Path(__file__).parent / "fixtures" / "avatar_vectors.json").read_text())["vectors"]


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def test_there_are_64_vectors():
    assert len(VECTORS) == 64


@pytest.mark.parametrize("v", VECTORS, ids=[v["id"][:8] for v in VECTORS])
def test_both_pictures_match_the_shared_vectors(v):
    assert sha(avatars.token_avatar(v["id"])) == v["token"]
    assert sha(avatars.everyday_avatar(v["id"])) == v["everyday"]


def test_a_picture_is_plain_flat_svg():
    for v in VECTORS:
        for svg in (avatars.token_avatar(v["id"]), avatars.everyday_avatar(v["id"], "x1")):
            assert svg.startswith("<svg ") and svg.endswith("</svg>")
            for bad in ("<script", "<text", "<image", "href", "<filter", "<style", "onload"):
                assert bad not in svg


def test_the_everyday_picture_takes_a_uid_for_its_pattern_ids():
    a, b = avatars.everyday_avatar(VECTORS[0]["id"], "one"), avatars.everyday_avatar(VECTORS[0]["id"], "two")
    assert 'id="aone"' in a and 'id="atwo"' in b and a != b


def test_the_token_picture_always_fits_the_registration_seam():
    biggest = 0
    for i in range(20000):
        uri = R.token_picture(sha(f"size-{i}"))
        biggest = max(biggest, len(uri))
    assert biggest <= 2048 and biggest < 800


def test_a_stable_id_with_0x_gives_the_same_picture():
    assert R.token_picture("0x" + STABLE) == R.token_picture(STABLE)
    with pytest.raises(R.RegisterError):
        R.token_picture("nope")


def _svg_of(file):
    head, b64 = file["image"].split(",", 1)
    assert head == "data:image/svg+xml;base64"
    return base64.b64decode(b64).decode()


def test_the_registration_carries_the_token_picture_by_default(target, fake):
    out = chain_register.build("base-sepolia", stable=STABLE)
    assert _svg_of(out["file"]) == avatars.token_avatar(STABLE)
    assert len(out["file"]["image"]) <= 2048
    assert out["picture_bytes"] == len(avatars.token_avatar(STABLE).encode())
    assert R.decode_data_uri(out["agent_uri"])["image"] == out["file"]["image"]


def test_no_image_leaves_it_out_and_the_estimate_is_for_the_file_sent(target, fake):
    with_img = chain_register.build("base-sepolia", stable=STABLE)
    without = chain_register.build("base-sepolia", stable=STABLE, no_image=True)
    assert "image" not in without["file"] and without["picture_bytes"] == 0
    assert len(with_img["calldata"]) > len(without["calldata"])
    gas_calls = [c for c in fake.calls if c[0] == "gas"]
    assert gas_calls[0][2] == bytes.fromhex(with_img["calldata"][2:]) and gas_calls[1][2] == bytes.fromhex(without["calldata"][2:])


def test_an_image_given_wins_over_the_default(target, fake):
    mine = "data:image/png;base64,AAAA"
    assert chain_register.build("base-sepolia", stable=STABLE, image=mine)["file"]["image"] == mine


def test_the_print_path_says_the_picture_is_included_and_its_size(target, fake, capsys):
    assert card.register_8004(target.name, "base-sepolia") == 0
    out = capsys.readouterr().out
    n = len(avatars.token_avatar(STABLE).encode())
    assert f"Picture: included, the agent's token picture ({n} bytes" in out
    assert '"image": "(the agent' in out        # the readable file does not print the long address


def test_no_image_on_the_command_says_it_is_left_out(target, fake, capsys):
    assert card.register_8004(target.name, "base-sepolia", no_image=True) == 0
    out = capsys.readouterr().out
    assert "Picture: left out (--no-image)" in out and "svg+xml" not in out
    assert card.register_8004(target.name, "base-sepolia", "0x" + "12" * 32, no_image=True) == 1


def test_the_cli_has_the_flag():
    from emporia import cli
    import inspect
    assert "--no-image" in inspect.getsource(cli)
