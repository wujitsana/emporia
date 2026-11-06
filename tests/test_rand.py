"""The randomness service `emporia:rand:v1`: the pinned drand chain, the BLS check, the draws, the
relay's beacon cache. Recorded beacons only: no test touches the network."""
import hashlib

from fastapi.testclient import TestClient

from emporia import beacon, rand, rand_client
from emporia import relay_server as rs
from emporia.relay_server import app
from emporia.routes import beacon as beacon_route

# Recorded from api.drand.sh on 2026-11-06 (chain 52db9b..., quicknet).
R1000 = {"round": 1000, "randomness": "fe290beca10872ef2fb164d2aa4442de4566183ec51c56ff3cd603d930e54fdd",
         "signature": "b44679b9a59af2ec876b1a6b1ad52ea9b1615fc3982b19576350f93447cb1125e342b73a8dd2bacbe47e4b6b63ed5e39"}
R_LATE = {"round": 33721081, "randomness": "437bcf8c1c97446b054f602b1c91be7f4a4afa73d1a883e8529797a243312aaf",
          "signature": "88e58caf24879ebe0263177e9c01b6b94b2265d2a5306e1d4b8206ba20ee7f3c759c39f3b8cb86b4aa396f5772738fed"}
RECORDED = {R1000["round"]: R1000, R_LATE["round"]: R_LATE}


def recorded_get(url: str):
    """What drand answers, from the recordings; an unrecorded round is a 404 (None)."""
    assert url.split("/")[-3] == rand.CHAIN_HASH, "the pinned chain hash is in every request"
    return RECORDED.get(int(url.rsplit("/", 1)[1]))


def test_the_pinned_chain_is_quicknet():
    assert rand.CHAIN_HASH == "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
    assert rand.SCHEME == "bls-unchained-g1-rfc9380" and rand.PERIOD_S == 3 and rand.GENESIS == 1692803367
    assert len(bytes.fromhex(rand.PUBLIC_KEY)) == 96, "a compressed G2 key"


def test_real_beacons_verify_in_pure_python_and_a_wrong_round_or_signature_does_not():
    for b in RECORDED.values():
        assert beacon.verify(b["round"], b["signature"])
        assert beacon.randomness(b["signature"]) == b["randomness"], "drand's randomness is sha256 of the signature"
    assert not beacon.verify(1001, R1000["signature"]), "a signature of another round"
    assert not beacon.verify(1000, R_LATE["signature"])
    flipped = R1000["signature"][:-2] + ("00" if R1000["signature"][-2:] != "00" else "01")
    assert not beacon.verify(1000, flipped)
    for junk in ("", "zz" * 48, "00" * 47, "c0" + "00" * 47, R1000["signature"].upper()):
        assert not beacon.verify(1000, junk), junk


def test_fetch_reads_and_verifies_each_source_and_skips_a_lying_one():
    assert beacon.fetch(1000, recorded_get) == R1000["signature"]
    assert beacon.fetch(999, recorded_get) is None

    def liar_then_truth(url):
        return {"round": 1000, "signature": R_LATE["signature"]} if "liar" in url else recorded_get(url)
    assert beacon.fetch(1000, liar_then_truth, bases=("https://liar.example", "https://api.drand.sh")) \
        == R1000["signature"]
    assert beacon.fetch(1000, lambda u: {"round": 1000, "signature": R_LATE["signature"]}) is None


def test_rounds_and_times():
    assert rand.round_time(1) == rand.GENESIS and rand.round_time(1001) == rand.GENESIS + 3000
    assert rand.round_at(rand.GENESIS) == 1 and rand.round_at(rand.GENESIS + 2.9) == 1 \
        and rand.round_at(rand.GENESIS + 3) == 2
    assert beacon.available(1000, now=rand.round_time(1000)) and not beacon.available(1000, now=rand.round_time(1000) - 1)


def test_the_draws_are_domain_separated_and_private_ones_need_the_beacon_and_the_secret():
    sig, s1, s2 = bytes.fromhex(R1000["signature"]), bytes(32), b"\x01" * 32
    base = rand.rand("sess_a", "board", sig)
    assert base == rand.rand("sess_a", "board", sig) and len(base) == 32
    assert base not in (rand.rand("sess_b", "board", sig), rand.rand("sess_a", "other", sig),
                        rand.rand("sess_a", "board", bytes.fromhex(R_LATE["signature"])))
    p = rand.priv("sess_a", 0, "hand", sig, s1)
    assert p != rand.priv("sess_a", 1, "hand", sig, s1) and p != rand.priv("sess_a", 0, "walk", sig, s1)
    assert p != rand.priv("sess_a", 0, "hand", sig, s2), "only the holder of s can compute it"
    assert p != rand.rand("sess_a", "hand", sig), "public and private draws never coincide"
    # fallback (beacon unreachable): the players' revealed secrets alone, order-free
    assert rand.rand("g", "x", b"", ["b", "a"]) == rand.rand("g", "x", b"", ["a", "b"])
    assert rand.rand("g", "x", b"", ["a", "b"]) != rand.rand("g", "x", b"", ["a", "c"])
    # no part list is ambiguous with another
    assert rand.rand("ab", "c", sig) != rand.rand("a", "bc", sig)


def test_a_commitment_is_sha256_of_secret_and_nonce():
    s, n = rand.new_secret()
    assert rand.commit_hash(bytes.fromhex(s), bytes.fromhex(n)) == hashlib.sha256(bytes.fromhex(s + n)).hexdigest()
    assert s != n and rand.is_hex(s, 32) and not rand.is_hex(s.upper(), 32) and not rand.is_hex(s[:-2], 32)


def test_a_round_must_be_at_least_ten_rounds_after_the_signing_time(monkeypatch):
    monkeypatch.setattr(rand, "MIN_LEAD_ROUNDS", 10)
    at = "2026-11-06T12:00:00+00:00"
    now_round = rand.round_at(rand.to_ts(at))
    assert rand.commit_round_problem(at, now_round + 10) == ""
    assert "too early" in rand.commit_round_problem(at, now_round + 9)
    assert "whole number" in rand.commit_round_problem(at, "x") and "whole number" in rand.commit_round_problem(at, True)
    c, rec = rand_client.make_commit(lambda *a: "sig", "sess_q", "me", 0, now=rand.to_ts(at))
    assert rand.commit_round_problem(c["at"], c["beacon_round"]) == "" and c["beacon_round"] == now_round + rand.CLIENT_LEAD_ROUNDS
    assert rec["beacon_round"] == c["beacon_round"] and rec["hash"] == c["hash"]


def test_a_client_that_saw_the_commitments_late_voids():
    rnd = 5000
    assert not rand_client.late_commit({"seen_all_at": rand.round_time(rnd) - 30}, rnd)
    assert rand_client.late_commit({"seen_all_at": rand.round_time(rnd) - 1}, rnd), "inside the margin"
    assert rand_client.late_commit({"seen_all_at": rand.round_time(rnd)}, rnd)
    assert not rand_client.late_commit({}, rnd)


# ── the relay's cache ────────────────────────────────────────────────────────

REAL_SIGNATURE = beacon_route.signature     # captured before the suite's autouse fake replaces it


def test_the_beacon_route_serves_a_verified_round_and_caches_it(monkeypatch):
    sig_fn = REAL_SIGNATURE
    calls = []

    def counting(url):
        calls.append(url)
        return recorded_get(url)
    monkeypatch.setattr(beacon_route, "_FETCH", counting)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("DELETE FROM beacon_rounds")
    assert sig_fn(1000) == R1000["signature"] and len(calls) == 1
    assert sig_fn(1000) == R1000["signature"] and len(calls) == 1, "second read is from the cache"
    assert sig_fn(2000) is None, "drand has no such recording: nothing is invented"
    far = rand.round_at(__import__("time").time()) + 10_000
    assert sig_fn(far) is None, "a round not published yet is not asked for"
    # served over HTTP with the same checks
    monkeypatch.setattr(beacon_route, "signature", sig_fn)
    client = TestClient(app, client=("127.0.0.1", 51232))
    r = client.get("/beacon/1000")
    assert r.status_code == 200 and r.json()["signature"] == R1000["signature"] and r.json()["chain"] == rand.CHAIN_HASH
    assert client.get("/beacon/0").status_code == 400
    r = client.get(f"/beacon/{far}")
    assert r.status_code == 404 and r.json()["detail"]["available_at"] == rand.round_time(far)
    assert client.get("/beacon/2000").status_code == 502


def test_a_cache_that_lies_is_caught_by_the_client_and_bypassed():
    good = {"round": 1000, "signature": R1000["signature"]}
    bad = {"round": 1000, "signature": R_LATE["signature"]}

    def get(url):
        return bad if "relay.example" in url else recorded_get(url) if "drand" in url else None
    assert rand_client.get_beacon(1000, "https://relay.example", get) == good["signature"], \
        "the relay's wrong value is refused and drand answers instead"
    assert rand_client.get_beacon(1000, "https://relay.example", lambda u: bad) is None
