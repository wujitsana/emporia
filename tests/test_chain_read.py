"""PinnedReader and rpc_for against a fake transport (no network)."""
import json

import httpx
import pytest

from emporia import chain_read, networks
from emporia.chain_read import ChainRevert, ChainUnknown, PinnedReader

H1, H2 = "0x" + "aa" * 32, "0x" + "bb" * 32
SECRET = "https://rpc.example/key-SECRET123"


class Fake:
    def __init__(self, chain=4663, finalized=True, latest=1000, status=200, big=False, exc=None,
                 call="0x1234", code="0x6001", revert=False, block_hash=H1, reject_hash=False):
        self.__dict__.update(locals())
        self.log = []

    def __call__(self, url, body, headers, timeout, max_bytes):
        req = json.loads(body)
        self.log.append((url, req["method"], req["params"], headers))
        if self.exc:
            raise self.exc
        m, p = req["method"], req["params"]
        res, err = None, None
        if m == "eth_chainId":
            res = hex(self.chain)
        elif m == "eth_getBlockByNumber" and p[0] == "finalized":
            if self.finalized:
                res = {"number": "0x10", "hash": self.block_hash}
            else:
                err = {"code": -32602, "message": "invalid block tag"}
        elif m == "eth_getBlockByNumber":
            res = {"number": p[0], "hash": self.block_hash}
        elif m == "eth_blockNumber":
            res = hex(self.latest)
        elif m in ("eth_call", "eth_getCode") and self.reject_hash and isinstance(p[-1], dict):
            err = {"code": -32602, "message": "invalid block number"}
        elif m == "eth_call":
            if self.revert:
                err = {"code": 3, "message": "execution reverted"}
            else:
                res = self.call
        elif m == "eth_getCode":
            res = self.code
        out = {"jsonrpc": "2.0", "id": req["id"], **({"error": err} if err else {"result": res})}
        data = json.dumps(out).encode()
        if self.big:
            data += b" " * (max_bytes + 1)
        return self.status, data


def reader(fake, urls=("https://a.example",), **kw):
    return PinnedReader("eip155:4663", list(urls), http=fake, **kw)


def test_pins_finalized_once_and_reads_at_it():
    f = Fake()
    r = reader(f)
    assert (r.block_number, r.block_hash) == (16, H1)
    assert r.call("0x" + "11" * 20, b"\x01\x02") == bytes.fromhex("1234")
    assert r.code("0x" + "11" * 20) == bytes.fromhex("6001")
    assert sum(1 for _, m, *_ in f.log if m == "eth_getBlockByNumber") == 1
    call = [x for x in f.log if x[1] == "eth_call"][0]
    assert call[2][1] == {"blockHash": H1} and call[2][0]["data"] == "0x0102"
    assert all(x[3]["User-Agent"] == chain_read.USER_AGENT for x in f.log)


def test_finalized_unsupported_falls_back_to_latest_minus_margin():
    r = reader(Fake(finalized=False, latest=1000))
    assert r.block_number == 1000 - chain_read.FINALITY_MARGIN


def test_wrong_chain_id():
    with pytest.raises(ChainUnknown, match="not chain 4663"):
        reader(Fake(chain=1)).pin()


def test_two_urls_must_agree():
    calls = {"a": Fake(call="0x01"), "b": Fake(call="0x02")}
    def http(url, *a):
        return calls[url[-1]](url, *a)
    r = reader(http, urls=("https://x/a", "https://x/b"))
    with pytest.raises(ChainUnknown, match="disagree"):
        r.call("0x" + "11" * 20, b"")
    # agreeing on the call but not the block
    calls = {"a": Fake(), "b": Fake(block_hash=H2)}
    with pytest.raises(ChainUnknown, match="disagree"):
        reader(http, urls=("https://x/a", "https://x/b")).pin()
    calls = {"a": Fake(), "b": Fake()}
    assert reader(http, urls=("https://x/a", "https://x/b")).block_hash == H1


def test_oversize_response():
    with pytest.raises(ChainUnknown, match="too large"):
        reader(Fake(big=True), max_response_bytes=500).pin()


def test_timeout_and_403_hide_the_url():
    req = httpx.Request("POST", SECRET)
    for exc in (httpx.ReadTimeout(f"timed out {SECRET}", request=req),
                httpx.HTTPStatusError(f"bad {SECRET}", request=req, response=httpx.Response(403, request=req))):
        with pytest.raises(ChainUnknown) as e:
            reader(Fake(exc=exc), urls=(SECRET,)).pin()
        assert "SECRET123" not in str(e.value) and "https://" not in str(e.value)
    with pytest.raises(ChainUnknown, match="HTTP 403") as e:
        reader(Fake(status=403), urls=(SECRET,)).pin()
    assert "SECRET123" not in str(e.value)


def test_revert_is_its_own_unknown_and_error_text_is_redacted():
    with pytest.raises(ChainRevert):
        reader(Fake(revert=True)).call("0x" + "11" * 20, b"")

    def http(*a):
        return 200, json.dumps({"error": {"code": -1, "message": f"see {SECRET}"}}).encode()
    with pytest.raises(ChainUnknown) as e:
        reader(http, urls=(SECRET,)).pin()
    assert "SECRET123" not in str(e.value)


def test_garbage_answers_are_unknown():
    for body in (b"not json", b"[]", json.dumps({"result": "zz"}).encode()):
        with pytest.raises(ChainUnknown):
            reader(lambda *a, b=body: (200, b)).pin()


def test_non_evm_chain_refused():
    with pytest.raises(ChainUnknown):
        PinnedReader("solana:abc", ["https://a"], http=Fake())


def test_rpc_for_precedence(monkeypatch):
    monkeypatch.delenv("EMPORIA_RPC_4663", raising=False)
    assert networks.rpc_for("eip155:4663") == ["https://rpc.mainnet.chain.robinhood.com"]
    monkeypatch.setenv("EMPORIA_RPC_4663", "https://one.example")
    assert networks.rpc_for("eip155:4663") == ["https://one.example"]
    monkeypatch.setenv("EMPORIA_RPC_4663", "https://one.example, https://two.example")
    assert networks.rpc_for("eip155:4663") == ["https://one.example", "https://two.example"]
    assert networks.rpc_for("eip155:999999") == []
    assert PinnedReader("eip155:4663", http=Fake()).urls == ["https://one.example", "https://two.example"]


def test_a_node_that_rejects_the_hash_form_is_read_by_number():
    f = Fake(reject_hash=True)
    r = reader(f)
    assert r.call("0x" + "11" * 20, b"\x01") == bytes.fromhex("1234")
    assert r.code("0x" + "11" * 20) == bytes.fromhex("6001")
    reads = [x[2][-1] for x in f.log if x[1] in ("eth_call", "eth_getCode")]
    assert reads == [{"blockHash": H1}, "0x10", "0x10"]   # one failed try, then number only


def test_a_revert_is_not_a_reason_to_retry_by_number():
    f = Fake(revert=True)
    with pytest.raises(ChainRevert):
        reader(f).call("0x" + "11" * 20, b"\x01")
    assert sum(1 for x in f.log if x[1] == "eth_call") == 1


def test_one_deadline_covers_the_whole_call(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(chain_read.time, "monotonic", lambda: now[0])
    f = Fake()
    seen = []

    def slow(url, body, headers, timeout, max_bytes):
        seen.append(timeout)
        now[0] += 4.0                      # every read takes 4 s of the 8 s budget
        return f(url, body, headers, timeout, max_bytes)

    r = reader(slow, timeout=8.0)
    with pytest.raises(ChainUnknown, match="too long"):
        r.call("0x" + "11" * 20, b"\x01")
    assert seen == [8.0, 4.0]              # each read gets only what is left
    assert max(seen) <= 8.0


def test_each_call_starts_a_fresh_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(chain_read.time, "monotonic", lambda: now[0])
    f = Fake()

    def fast(*a):
        now[0] += 1.0
        return f(*a)

    r = reader(fast, timeout=8.0)
    for _ in range(4):
        r.code("0x" + "11" * 20)   # the first pins (4 reads) and reads; later ones read once
