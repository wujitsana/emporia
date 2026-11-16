"""The local console's own look at an agent's ERC-8004 link (docs/protocol.md §3.20, plan §6).

The relay's card says what the relay read. This reads the chain itself, from this machine, over the RPCs in
`networks.py` (or `EMPORIA_RPC_<CHAINID>`), at one pinned block, and cached for ten minutes: "checked by you at block
N" is the one status the viewer need not take on anyone's word. It also shows the agent's registration text and image
-- both a stranger's, so both are treated as hostile:

* text: capped, NFKC-normalised, control and format characters removed, flagged by the injection firewall; it is
  never the agent's name and never an instruction;
* image: only an on-chain `data:` URI (the bound token's art when it is held by the signer, else the registration's own
  image); caps of 64 KB (registration), 1 MB (token JSON) and 256 KB (image); the type decided by the bytes' magic
  number, never the declared one; an SVG with script, a foreign object, an external reference or an entity is refused;
  a remote URL (https, ipfs) is never fetched. The route that serves it sets `Content-Security-Policy: sandbox;
  default-src 'none'` and `nosniff`; the page shows it in an `<img>`, never inline, and never an `animation_url`.

Reads only. Nothing here signs, writes or sends anything to the relay.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable
from urllib.parse import unquote_to_bytes

from emporia import erc8004, networks
from emporia.erc8004_register import names_agent
from emporia.chain_read import ChainRevert, ChainUnknown, PinnedReader

CACHE_SECONDS = 600
REGISTRATION_CAP = 64 * 1024
TOKEN_JSON_CAP = 1024 * 1024
IMAGE_CAP = 256 * 1024
NAME_CAP, DESCRIPTION_CAP = 80, 400
SEL_TOKEN_URI = bytes.fromhex("c87b56dd")      # tokenURI(uint256)
RESPONSE_CAP = 4 * 1024 * 1024                 # a token's `data:` JSON arrives hex-encoded and base64-inflated


class Refused(Exception):
    """Not shown, and why (the words are the viewer's)."""


# -- text ------------------------------------------------------------------------------------------
def clean_text(value: Any, cap: int) -> str:
    """A stranger's text as shown: a string only, NFKC-normalised, no control or format characters (no bidi tricks,
    no zero-width), whitespace collapsed, cut at `cap` with an ellipsis."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = "".join(" " if c.isspace() else c for c in text if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn")
                   or c.isspace())
    text = re.sub(r" +", " ", text).strip()
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def stranger_text(value: Any, cap: int) -> tuple[str, list[str]]:
    """(the cleaned text, its injection flags)."""
    from emporia.engine.guardrails import injection_flags
    text = clean_text(value, cap)
    return text, (injection_flags(text) if text else [])


# -- data: URIs and images ---------------------------------------------------------------------------
def parse_data_uri(uri: Any, cap: int) -> tuple[str, bytes]:
    """(declared media type, bytes) of a `data:` URI, at most `cap` bytes decoded; anything else is refused. A
    remote URL is never fetched: it is refused here."""
    if not isinstance(uri, str) or not uri[:5].lower() == "data:":
        raise Refused("not an on-chain data: URI (a remote image is not loaded)")
    head, sep, body = uri[5:].partition(",")
    if not sep:
        raise Refused("a malformed data: URI")
    params = [p.strip() for p in head.split(";")]
    mime = params[0].lower() or "text/plain"
    if len(body) > cap * 4 + 16:
        raise Refused(f"larger than {cap // 1024} KB")
    try:
        if any(p.lower() == "base64" for p in params[1:]):
            data = base64.b64decode(re.sub(r"\s+", "", body), validate=True)
        else:
            data = unquote_to_bytes(body)
    except (binascii.Error, ValueError):
        raise Refused("a malformed data: URI") from None
    if len(data) > cap:
        raise Refused(f"larger than {cap // 1024} KB")
    return mime, data


_SVG_BAD = re.compile(
    rb"<\s*script|<\s*foreignObject|<\s*iframe|<\s*embed|<\s*object|<\s*image\b|<\s*audio|<\s*video"
    rb"|<!ENTITY|<!DOCTYPE|javascript\s*:|vbscript\s*:|\bon[a-z]+\s*=|@import"
    rb"|(?:xlink:)?href\s*=\s*[\"']?\s*(?!#)|url\s*\(\s*[\"']?\s*(?!#)", re.I)


def sniff_image(data: bytes) -> str:
    """The image type by its first bytes (never the declared type): png, jpeg, gif, webp or svg; else Refused.
    An SVG is accepted only when nothing in it can run, load or reference anything outside itself."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:512].lower()
    if head.startswith((b"<svg", b"<?xml", b"<!--")) and b"<svg" in data[:2048].lower():
        if _SVG_BAD.search(data):
            raise Refused("an SVG that can run or load something is not shown")
        return "image/svg+xml"
    raise Refused("not a PNG, JPEG, GIF, WebP or plain SVG image")


def image_of(uri: Any) -> tuple[str, bytes]:
    mime, data = parse_data_uri(uri, IMAGE_CAP)
    return sniff_image(data), data


# -- the read ------------------------------------------------------------------------------------------
@dataclass
class Loaded:
    status: str                                  # checked | not-proven | unknown | not-checked
    reason: str = ""
    tier: str = ""
    owner: str = ""
    binding: dict | None = None
    block_number: int | None = None
    block_hash: str = ""
    checked_at: int = 0
    registration: dict | None = None
    image_source: str = "none"                  # bound-token | registration | none
    image_note: str = ""
    image: tuple[str, bytes] | None = field(default=None, repr=False)
    signature_checked: bool = False             # the wallet's signature was verified here too, not only the chain
    two_way: bool = False                       # the on-chain registration's Emporia service names this agent

    def view(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "tier": self.tier,
                "tier_wording": erc8004.TIER_WORDING.get(self.tier, ""), "owner": self.owner, "binding": self.binding,
                "block_number": self.block_number, "block_hash": self.block_hash, "checked_at": self.checked_at,
                "signature_checked": self.signature_checked, "two_way": self.two_way, "registration": self.registration,
                "image": {"source": self.image_source, "available": self.image is not None, "note": self.image_note,
                          "type": self.image[0] if self.image else ""}}


class ChainViews:
    """One per console. `reader(caip2)` gives a pinned reader (a test puts an in-memory chain there)."""

    def __init__(self, reader: Callable[[str], Any] | None = None, clock: Callable[[], float] = time.time):
        self._reader = reader or (lambda caip2: PinnedReader(caip2, max_response_bytes=RESPONSE_CAP))
        self._clock = clock
        self._cache: dict[tuple[str, str, str, str], tuple[float, Loaded]] = {}

    def load(self, caip2: str, agent_id: str, signer: str, signed: dict | None = None) -> tuple[Loaded, bool]:
        """(the look, whether it came from the cache). Blocking: run it in a thread. `signed` is the card's
        `{stable_id, issued_at, wallet_sig}`: with it the wallet's signature is verified here as well as the
        chain's control (`erc8004.verify`); without it only the chain is read (`erc8004.preview`)."""
        key = (caip2, str(int(agent_id)), signer.lower(),
               "" if not signed else f"{signed['stable_id']}/{signed['issued_at']}/{signed['wallet_sig']}")
        hit = self._cache.get(key)
        now = self._clock()
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1], True
        got = self._read(caip2, key[1], signer, int(now), signed)
        if got.status != "unknown":             # an RPC error is not remembered: the next look tries again
            self._cache[key] = (now, got)
        return got, False

    def _read(self, caip2: str, agent_id: str, signer: str, now: int, signed: dict | None = None) -> Loaded:
        if not erc8004.available():
            return Loaded("not-checked", "no signature library is installed here", checked_at=now)
        try:
            reader = self._reader(caip2)
            if signed:
                link = {"agent_registry": erc8004.registry_string(int(caip2.split(":", 1)[1]),
                                                                   networks.ERC8004_REGISTRIES[caip2]),
                        "agent_id": agent_id, "signer": signer, **signed}
                v = erc8004.verify(link, reader, now=now)
                if v["status"] in ("unknown", "not-checked"):
                    return Loaded(v["status"], v["reason"], checked_at=now)
                ok = v["status"] == "proven"
                got = Loaded("checked" if ok else "not-proven", "" if ok else v["reason"], v["tier"] if ok else "",
                             v["owner"], v.get("binding"), v["block_number"], v["block_hash"], v["checked_at"],
                             signature_checked=True)
                if not ok:
                    return got
                try:
                    self._show(reader, caip2, agent_id, signer, got, signed["stable_id"])
                except ChainUnknown:
                    got.image_note = "the registration could not be read now"
                return got
            pv = erc8004.preview(agent_id, signer, reader, now=now)
        except ChainUnknown as e:
            return Loaded("unknown", f"the chain could not be read ({e})", checked_at=now)
        got = Loaded("checked" if pv["tier"] else "not-proven", pv.get("reason", ""), pv["tier"], pv["owner"],
                     pv.get("binding"), pv["block_number"], pv["block_hash"], pv["checked_at"])
        if not pv["tier"]:
            return got
        try:
            self._show(reader, caip2, agent_id, signer, got)
        except ChainUnknown:
            got.image_note = "the registration could not be read now"
        return got

    # -- what the agent says of itself (a stranger's words, and its picture) ---------------------------
    def _token_uri(self, reader: Any, to: str, token: int) -> str:
        from eth_abi import decode, encode
        try:
            raw = reader.call(to, SEL_TOKEN_URI + encode(["uint256"], [token]))
            (uri,) = decode(["string"], raw)
        except ChainRevert:
            raise Refused("no token URI") from None
        except ChainUnknown:
            raise
        except Exception:  # noqa: BLE001 — any parse failure becomes Refused, the one answer the caller handles
            raise Refused("an unreadable token URI") from None
        return uri

    def _token_json(self, reader: Any, to: str, token: int, cap: int, uri: str | None = None) -> dict:
        if uri is None:
            uri = self._token_uri(reader, to, token)
        mime, data = parse_data_uri(uri, cap)
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Refused("not JSON") from None
        if not isinstance(doc, dict):
            raise Refused("not a JSON object")
        return doc

    def _show(self, reader: Any, caip2: str, agent_id: str, signer: str, got: Loaded, stable: str = "") -> None:
        reg_image: Any = None
        try:
            registry = networks.ERC8004_REGISTRIES[caip2]
            uri = self._token_uri(reader, registry, int(agent_id))
            # two-way: the registration, read at the same pinned block, names this agent's stable id. Only an
            # on-chain data: URI counts; a remote URL is never fetched.
            got.two_way = bool(stable) and names_agent(uri, stable)
            doc = self._token_json(reader, registry, int(agent_id), REGISTRATION_CAP, uri)
            name, nflags = stranger_text(doc.get("name"), NAME_CAP)
            desc, dflags = stranger_text(doc.get("description"), DESCRIPTION_CAP)
            got.registration = {"name": name, "description": desc, "flags": sorted(set(nflags + dflags)),
                                "active": doc.get("active") if isinstance(doc.get("active"), bool) else None,
                                "stranger_text": True}
            reg_image = doc.get("image")
        except Refused as e:
            got.registration = {"note": f"the registration is not shown: {e}"}
        # (a) the bound token's art, only when this machine's read says the signer holds it
        b = got.binding
        if got.tier == "bound-owner" and b and b.get("standard") == "erc721" and "token_id" in b:
            try:
                art = self._token_json(reader, b["address"], int(b["token_id"]), TOKEN_JSON_CAP)
                got.image, got.image_source = image_of(art.get("image")), "bound-token"     # never `animation_url`
                return
            except Refused as e:
                got.image_note = f"the bound token's image is not shown: {e}"
        # (b) the registration's own, only when it is on-chain
        if reg_image:
            try:
                got.image, got.image_source = image_of(reg_image), "registration"
                return
            except Refused as e:
                got.image_note = ("image hosted off-chain" if isinstance(reg_image, str) and reg_image[:5].lower() != "data:"
                                  else f"the registration's image is not shown: {e}")
        elif not got.image_note:
            got.image_note = "no image"
