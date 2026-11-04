"""An agent's side of the signer (P203): its wallet calls, sent to the service
that holds its keys. `EMPORIA_SIGNER_URL` is `unix:///path/signer.sock` or
`http://127.0.0.1:PORT`; `EMPORIA_SIGNER_TOKEN` says which agent asks."""

from __future__ import annotations

import os
import time

import httpx

_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_TTL_S = 30


class Client:
    def __init__(self, url: str, token: str, agent_id: str):
        self.url, self.token, self.agent_id = url.strip(), token.strip(), agent_id.strip()

    @classmethod
    def from_env(cls) -> Client:
        return cls(os.getenv("EMPORIA_SIGNER_URL", ""), os.getenv("EMPORIA_SIGNER_TOKEN", ""),
                   os.getenv("EMPORIA_AGENT_ID", ""))

    def _http(self) -> httpx.Client:
        if self.url.startswith("unix://"):
            return httpx.Client(transport=httpx.HTTPTransport(uds=self.url.removeprefix("unix://")),
                                base_url="http://signer", timeout=60)
        return httpx.Client(base_url=self.url.rstrip("/"), timeout=60)

    def _headers(self) -> dict:
        return {"authorization": f"Bearer {self.token}", "x-emporia-agent": self.agent_id}

    def _fail(self, what: str, detail) -> None:
        from emporia.agent_wallet import AgentWalletError
        msg = detail.get("error") if isinstance(detail, dict) else str(detail)
        raise AgentWalletError(f"the signer refused ({what}): {msg}")

    def health(self) -> dict:
        with self._http() as h:
            return h.get("/health").json()

    def wallet(self, fresh: bool = False) -> dict:
        key = (self.url, self.agent_id)
        hit = _CACHE.get(key)
        if hit and not fresh and time.time() - hit[0] < _TTL_S:
            return hit[1]
        try:
            with self._http() as h:
                r = h.get("/v1/wallet", headers=self._headers())
        except httpx.HTTPError as e:
            self._fail("wallet", f"not reachable at {self.url} — {e}")
        if r.status_code != 200:
            self._fail("wallet", r.json().get("detail", r.text))
        _CACHE[key] = (time.time(), r.json())
        return r.json()

    def call(self, op: str, payload: dict) -> dict:
        try:
            with self._http() as h:
                r = h.post(f"/v1/{op}", json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            self._fail(op, f"not reachable at {self.url} — {e}")
        if r.status_code != 200:
            self._fail(op, r.json().get("detail", r.text))
        _CACHE.pop((self.url, self.agent_id), None)     # spent today changed
        return r.json()


    # ─── P293 (signer phase 2a): signatures, by kind ─────────────────────────

    def _signing(self, path: str, body: dict) -> dict:
        from emporia import signing
        try:
            with self._http() as h:
                r = h.post(path, json=body, headers=self._headers())
        except httpx.HTTPError as e:
            raise signing.Refused(f"the signer is not reachable at {self.url} — {e}") from None
        detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith(
            "application/json") else r.text
        msg = detail.get("error") if isinstance(detail, dict) else str(detail)
        if r.status_code == 429:
            raise signing.Limited(f"the signer: {msg}", float((detail or {}).get("retry_after", 60)))
        if r.status_code != 200:
            raise signing.Refused(f"the signer: {msg}")
        return r.json()

    def sign(self, kind: str, args: list, kwargs: dict) -> str:
        return self._signing("/v1/sign", {"kind": kind, "args": args, "kwargs": kwargs})["signature"]

    def sign_in(self, nonce: str) -> str:
        return self._signing("/v1/sign-in", {"nonce": nonce})["signature"]

    def identity(self) -> dict:
        with self._http() as h:
            r = h.get("/v1/identity", headers=self._headers())
        if r.status_code != 200:
            self._fail("identity", r.json().get("detail", r.text))
        return r.json()


    def open(self, envelope: dict, label: str, aad: bytes) -> str:
        """P293 (2b): the plaintext of one sealed message, opened by the signer."""
        import base64

        from emporia.crypto_box import DecryptionError
        try:
            with self._http() as h:
                r = h.post("/v1/open", json={"envelope": envelope, "label": label,
                                             "aad": base64.b64encode(aad).decode()}, headers=self._headers())
        except httpx.HTTPError as e:
            raise DecryptionError(f"the signer is not reachable at {self.url} — {e}") from None
        if r.status_code != 200:
            detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith(
                "application/json") else r.text
            raise DecryptionError(f"the signer did not open it: "
                                  f"{detail.get('error') if isinstance(detail, dict) else detail}")
        return r.json()["plaintext"]


class Opener:
    """What `identity.encryption_private_keys` gives for an agent whose encryption
    keys are in its signer: `crypto_box.open_sealed` hands it the envelope and the
    context it was sealed to, and the signer opens it. No key on this side."""

    def __init__(self, client: Client):
        self.client = client

    @classmethod
    def for_agent(cls, agent_id: str) -> Opener:
        from emporia.identity import IdentityInSigner, _keyfile_stem
        client = Client.from_env()
        if not client.url or _keyfile_stem(client.agent_id) != _keyfile_stem(agent_id):
            raise IdentityInSigner(f"{agent_id}'s encryption keys are in its signer, but this process is set up "
                                   f"to ask it as {client.agent_id or 'no agent'}")
        return cls(client)

    def open_sealed(self, envelope: dict, agent_id: str, aad: bytes) -> str:
        return self.client.open(envelope, agent_id, aad)


class Admin:
    """The operator's side: pending approvals. Needs the admin token, which only
    the signer's user can read."""

    def __init__(self, url: str, token: str):
        self.client = Client(url, token, "")

    def intents(self) -> list[dict]:
        with self.client._http() as h:
            r = h.get("/admin/intents", headers={"authorization": f"Bearer {self.client.token}"})
        r.raise_for_status()
        return r.json()["intents"]

    def log(self, agent_id: str = "", limit: int = 50) -> list[dict]:
        """P293: what the signer signed and refused — kinds, results, reasons; never content."""
        with self.client._http() as h:
            r = h.get("/admin/log", params={"agent": agent_id, "limit": limit},
                      headers={"authorization": f"Bearer {self.client.token}"})
        r.raise_for_status()
        return r.json()["entries"]

    def decide(self, intent_id: str, approve: bool) -> dict:
        with self.client._http() as h:
            r = h.post(f"/admin/intents/{intent_id}/{'approve' if approve else 'deny'}",
                       headers={"authorization": f"Bearer {self.client.token}"})
        if r.status_code != 200:
            raise RuntimeError(r.json().get("detail", r.text))
        return r.json()
