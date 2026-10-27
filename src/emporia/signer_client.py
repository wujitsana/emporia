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

    def decide(self, intent_id: str, approve: bool) -> dict:
        with self.client._http() as h:
            r = h.post(f"/admin/intents/{intent_id}/{'approve' if approve else 'deny'}",
                       headers={"authorization": f"Bearer {self.client.token}"})
        if r.status_code != 200:
            raise RuntimeError(r.json().get("detail", r.text))
        return r.json()
