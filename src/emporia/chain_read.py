"""Read-only chain reads at one pinned block (the plumbing for on-chain verification).

The one exception is `send_raw` (`eth_sendRawTransaction`): only the person-run `register-8004` / `set-8004-wallet
--from-agent-wallet` commands call it, on Base Sepolia, with the agent's own wallet (`chain_send`).

`PinnedReader` talks JSON-RPC to one or two URLs for a CAIP-2 chain: it checks the chain id, pins one block
once, and answers `call` / `code` at that block. A failed read is `ChainUnknown`, never a guess, and its text
never carries a URL (an RPC URL may hold a provider's key).
"""
from __future__ import annotations

import json
import time
from typing import Any
from collections.abc import Callable

from emporia import rpc_safe
from emporia.networks import rpc_for

USER_AGENT = "emporia-chain-read/1"
FINALITY_MARGIN = 12  # blocks behind latest, when the RPC has no `finalized` tag
# http(url, body, headers, timeout, max_bytes) -> (status, response bytes)
Http = Callable[[str, bytes, dict, float, int], "tuple[int, bytes]"]


class ChainUnknown(Exception):
    """The chain could not be read (or its answer was not trustworthy). Never `invalid`."""


class ChainRpcError(ChainUnknown):
    """The node answered with a JSON-RPC error (not a revert): it may just not know a parameter form."""

    def __init__(self, message: str, code: Any = None):
        super().__init__(message)
        self.code = code

    def rejects_block_hash_form(self) -> bool:
        """The error says the node does not take `{"blockHash": ...}` (invalid params or request, or its own
        words for it), as against an error about the block itself ("header not found" on a node that lags)."""
        text = str(self).lower()
        return self.code in (-32602, -32600) or "blockhash" in text or "1898" in text or "invalid argument" in text


class ChainRevert(ChainUnknown):
    """The node answered that the call reverted."""


def _httpx_post(url: str, body: bytes, headers: dict, timeout: float, max_bytes: int) -> tuple[int, bytes]:
    import httpx

    end = time.monotonic() + timeout   # httpx's timeout is per read: a slow drip needs a whole-response one too
    with httpx.stream("POST", url, content=body, headers=headers, timeout=timeout) as r:
        data = bytearray()
        for chunk in r.iter_bytes():
            data += chunk
            if time.monotonic() > end:
                raise ChainUnknown("the RPC was too slow")
            if len(data) > max_bytes:
                raise ChainUnknown("the RPC response was too large")
        return r.status_code, bytes(data)


def _hex(n: int) -> str:
    return hex(n)


class PinnedReader:
    def __init__(self, caip2: str, urls: list[str] | None = None, *, timeout: float = 8.0,
                 max_response_bytes: int = 256 * 1024, gas_cap: int = 5_000_000, http: Http | None = None,
                 tag: str = "finalized"):
        if not caip2.startswith("eip155:"):
            raise ChainUnknown(f"not an EVM chain: {caip2}")
        try:
            self.chain_id = int(caip2.split(":", 1)[1])
        except ValueError:
            raise ChainUnknown(f"not an EVM chain: {caip2}") from None
        if tag not in ("finalized", "safe"):
            raise ChainUnknown(f"unknown block tag {tag!r}")
        self.tag = tag                  # which block the reader pins: `finalized`, or `safe` (minutes, not a quarter hour)
        self.caip2 = caip2
        self.urls = list(urls) if urls else rpc_for(caip2)
        if not self.urls:
            raise ChainUnknown(f"no RPC for {caip2}")
        self.timeout, self.max_bytes, self.gas_cap = timeout, max_response_bytes, gas_cap
        self._http: Http = http or _httpx_post
        self._n = 0
        self._block: tuple[int, str] | None = None
        self._by_hash = True            # EIP-1898 object form, until a node rejects it
        self._until: float | None = None  # the deadline of the call in progress

    def _begin(self) -> bool:
        """Start the one overall deadline for a public call (False if an outer call already has it)."""
        if self._until is not None:
            return False
        self._until = time.monotonic() + self.timeout
        return True

    # -- transport ----------------------------------------------------------------------------------
    def _rpc(self, url: str, method: str, params: list) -> Any:
        self._n += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._n, "method": method, "params": params}).encode()
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        left = self.timeout if self._until is None else min(self.timeout, self._until - time.monotonic())
        if left <= 0:
            raise ChainUnknown("the RPC took too long")
        try:
            status, data = self._http(url, body, headers, left, self.max_bytes)
        except ChainUnknown:
            raise
        except Exception as e:  # noqa: BLE001 — every RPC failure becomes ChainUnknown (the caller's one 'cannot tell' answer)
            raise ChainUnknown(rpc_safe.describe(e)) from None
        if status != 200:
            raise ChainUnknown(f"the RPC request failed (HTTP {status})")
        if len(data) > self.max_bytes:
            raise ChainUnknown("the RPC response was too large")
        try:
            msg = json.loads(data)
        except ValueError:
            raise ChainUnknown("the RPC answered something that is not JSON") from None
        if not isinstance(msg, dict):
            raise ChainUnknown("the RPC answered something unexpected")
        err = msg.get("error")
        if err:
            text = rpc_safe.redact(err.get("message", "") if isinstance(err, dict) else err)[:120]
            if isinstance(err, dict) and (err.get("code") == 3 or "revert" in text.lower()):
                raise ChainRevert(f"the call reverted: {text}")
            raise ChainRpcError(f"the RPC returned an error: {text}", err.get("code") if isinstance(err, dict) else None)
        return msg.get("result")

    def _all(self, method: str, params: list) -> Any:
        """The same read on every URL; with two, both must agree."""
        results = [self._rpc(u, method, params) for u in self.urls]
        if any(r != results[0] for r in results[1:]):
            raise ChainUnknown("the RPCs disagree")
        return results[0]

    # -- pinning ------------------------------------------------------------------------------------
    @staticmethod
    def _parse_block(b: Any) -> tuple[int, str] | None:
        if not isinstance(b, dict):
            return None
        try:
            number, bhash = int(b["number"], 16), b["hash"]
        except (KeyError, TypeError, ValueError):
            return None
        if not (isinstance(bhash, str) and len(bhash) == 66 and bhash.startswith("0x")):
            return None
        return number, bhash.lower()

    def pin(self) -> tuple[int, str]:
        if self._block is not None:
            return self._block
        own = self._begin()
        try:
            return self._pin()
        finally:
            if own:
                self._until = None

    def _pin(self) -> tuple[int, str]:
        for url in self.urls:
            cid = self._rpc(url, "eth_chainId", [])
            try:
                ok = int(cid, 16) == self.chain_id
            except (TypeError, ValueError):
                ok = False
            if not ok:
                raise ChainUnknown(f"the RPC is not chain {self.chain_id}")
        block = None
        try:
            block = self._parse_block(self._rpc(self.urls[0], "eth_getBlockByNumber", [self.tag, False]))
        except ChainUnknown:
            block = None  # the tag is unsupported: fall back to latest minus a margin
        if block is None:
            latest = self._rpc(self.urls[0], "eth_blockNumber", [])
            try:
                n = max(int(latest, 16) - FINALITY_MARGIN, 0)
            except (TypeError, ValueError):
                raise ChainUnknown("the RPC answered no block number") from None
            block = self._parse_block(self._rpc(self.urls[0], "eth_getBlockByNumber", [_hex(n), False]))
            if block is None:
                raise ChainUnknown("the RPC answered no block")
        for url in self.urls[1:]:
            other = self._parse_block(self._rpc(url, "eth_getBlockByNumber", [_hex(block[0]), False]))
            if other != block:
                raise ChainUnknown("the RPCs disagree")
        self._block = block
        return block

    @property
    def block_number(self) -> int:
        return self.pin()[0]

    @property
    def block_hash(self) -> str:
        return self.pin()[1]

    # -- reads --------------------------------------------------------------------------------------
    @staticmethod
    def _bytes(result: Any) -> bytes:
        if not isinstance(result, str) or not result.startswith("0x"):
            raise ChainUnknown("the RPC answered something unexpected")
        try:
            return bytes.fromhex(result[2:])
        except ValueError:
            raise ChainUnknown("the RPC answered something unexpected") from None

    def _at_block(self, method: str, head: list) -> Any:
        """`method(*head, block)` at the pinned block: by hash (EIP-1898) where the nodes take it, else by number."""
        own = self._begin()
        try:
            number, bhash = self.pin()
            if self._by_hash:
                try:
                    return self._all(method, [*head, {"blockHash": bhash}])
                except ChainRevert:
                    raise
                except ChainRpcError as e:
                    if not e.rejects_block_hash_form():
                        raise    # an error about the block (a node that lags): not a reason to leave the hash form
                    self._by_hash = False   # a node rejected the object form: number from here on
            return self._all(method, [*head, _hex(number)])
        finally:
            if own:
                self._until = None

    def call(self, to: str, data: bytes | str) -> bytes:
        payload = data if isinstance(data, str) else "0x" + bytes(data).hex()
        tx = {"to": to, "data": payload, "gas": _hex(self.gas_cap)}
        return self._bytes(self._at_block("eth_call", [tx]))

    def code(self, address: str) -> bytes:
        return self._bytes(self._at_block("eth_getCode", [address]))

    # -- not pinned: what a person's wallet has done, or would do, now ---------------------------------
    def _first(self, method: str, params: list) -> Any:
        """One read on the first URL, at no pinned block (a quote or a receipt, not a fact the verifier relies on)."""
        own = self._begin()
        try:
            return self._rpc(self.urls[0], method, params)
        finally:
            if own:
                self._until = None

    def simulate(self, to: str, data: bytes | str, sender: str) -> bytes:
        """`eth_call` of a transaction from `sender` at the latest block: what it would return. A revert is ChainRevert."""
        payload = data if isinstance(data, str) else "0x" + bytes(data).hex()
        return self._bytes(self._first("eth_call", [{"from": sender, "to": to, "data": payload}, "latest"]))

    def estimate_gas(self, to: str, data: bytes | str, sender: str) -> int:
        payload = data if isinstance(data, str) else "0x" + bytes(data).hex()
        got = self._first("eth_estimateGas", [{"from": sender, "to": to, "data": payload}])
        try:
            return int(got, 16)
        except (TypeError, ValueError):
            raise ChainUnknown("the RPC answered something unexpected") from None

    def receipt(self, tx_hash: str) -> dict | None:
        """The receipt of a mined transaction, or None while it is not mined (on the first URL)."""
        got = self._first("eth_getTransactionReceipt", [tx_hash])
        if got is None:
            return None
        if not isinstance(got, dict):
            raise ChainUnknown("the RPC answered something unexpected")
        return got

    def block_hash_at(self, number: int) -> str:
        """The hash of block `number` on the first URL (to check a receipt's block is the one pinned)."""
        b = self._parse_block(self._first("eth_getBlockByNumber", [_hex(number), False]))
        if b is None:
            raise ChainUnknown("the RPC answered no block")
        return b[1]

    # -- what sending a transaction from an agent's own wallet needs (all on the first URL, none pinned) ----------
    def _int(self, method: str, params: list) -> int:
        got = self._first(method, params)
        try:
            return int(got, 16)
        except (TypeError, ValueError):
            raise ChainUnknown("the RPC answered something unexpected") from None

    def balance(self, address: str) -> int:
        """The account's balance in wei, now."""
        return self._int("eth_getBalance", [address, "latest"])

    def nonce(self, address: str, tag: str = "pending") -> int:
        """The account's next transaction number: `pending` counts what is waiting to be mined, `latest` only what is."""
        if tag not in ("pending", "latest"):
            raise ChainUnknown(f"unknown block tag {tag!r}")
        return self._int("eth_getTransactionCount", [address, tag])

    def fee_data(self) -> tuple[int, int]:
        """(suggested priority fee, the latest block's base fee), in wei: what an EIP-1559 fee is built from."""
        own = self._begin()
        try:
            priority = self._int("eth_maxPriorityFeePerGas", [])
            block = self._first("eth_getBlockByNumber", ["latest", False])
            try:
                base = int(block["baseFeePerGas"], 16)
            except (KeyError, TypeError, ValueError):
                raise ChainUnknown("the chain answered no base fee (not an EIP-1559 chain?)") from None
            return priority, base
        finally:
            if own:
                self._until = None

    def chain_time(self) -> int:
        """The latest block's timestamp (what a contract's `block.timestamp` is about to be)."""
        block = self._first("eth_getBlockByNumber", ["latest", False])
        try:
            return int(block["timestamp"], 16)
        except (KeyError, TypeError, ValueError):
            raise ChainUnknown("the RPC answered no block") from None

    def send_raw(self, raw: bytes | str) -> str:
        """`eth_sendRawTransaction` on the first URL: the transaction hash the node answers. The one write this
        class has; only the person-run send commands call it, one transaction per run."""
        payload = raw if isinstance(raw, str) else "0x" + bytes(raw).hex()
        got = self._first("eth_sendRawTransaction", [payload])
        if not isinstance(got, str) or len(got) != 66 or not got.startswith("0x"):
            raise ChainUnknown("the RPC answered something unexpected")
        return got.lower()
