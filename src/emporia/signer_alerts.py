"""The signer's own alerts to its person (signer phase 2, section 5).

The signer tells its person when it refuses something or when an agent's
identity changes (a new payout wallet, new home or backups, a new encryption
key). It does this itself, straight to a channel whose credentials sit in the
signer's own folder (`alerts.json`, 0600). Alerts never go through the agent's
chat: an agent that has been taken over controls that chat and could silence
or forge what is said there.

Telegram is the first channel; `CHANNELS` maps a channel name to a send
function `(cfg, text) -> None`, so another can be added by registering one.
Routine signatures are not alerted. Sending never raises into the caller and
never holds up signing: `notify` hands each send to a daemon thread and writes
the outcome to the signer's audit log (without the token).
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable

import httpx

from emporia import identity, signer

EVENTS = ("refused", "payout-wallet", "home-or-backups", "encryption-key", "open-burst", "test",
          "identity-change", "recovery-key")     # P299: its person rotated its key, made a recovery key
MAX_PER_HOUR = 20
DETAIL_MAX = 300
_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{35,}$")
_CHAT_RE = re.compile(r"^(-?\d+|@[A-Za-z][A-Za-z0-9_]{3,})$")
_SECRET_RE = re.compile(r"[0-9a-fA-F]{64,}|\b(?:sk_|whsec_)[A-Za-z0-9_]*")
_LOCK = threading.Lock()
_SENT: dict[str, list[float]] = {}
_CAPPED: dict[str, float] = {}


class AlertError(Exception):
    """A channel could not deliver. The message never holds a token."""


# ─── config ──────────────────────────────────────────────────────────────────

def _path():
    return signer.signer_dir() / "alerts.json"


def config() -> dict:
    cfg = signer._read_json(_path(), {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("channels", {})
    cfg.setdefault("daily_summary", False)
    return cfg


def _save(cfg: dict) -> None:
    signer.signer_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    identity._write_secret_atomically(_path(), json.dumps(cfg, indent=1).encode())
    signer.own(_path())


def set_telegram(token: str, chat_id: str) -> None:
    token, chat_id = str(token).strip(), str(chat_id).strip()
    if not _TOKEN_RE.match(token):
        raise ValueError("That does not look like a Telegram bot token (expected <digits>:<35 or more letters, "
                         "digits, - or _>).")
    if not _CHAT_RE.match(chat_id):
        raise ValueError("The chat id must be a whole number (negative for groups) or an @channelname.")
    cfg = config()
    cfg["channels"]["telegram"] = {"token": token, "chat_id": chat_id}
    _save(cfg)


def clear(channel: str) -> None:
    cfg = config()
    if cfg["channels"].pop(channel, None) is not None:
        _save(cfg)


def channels() -> list[str]:
    return sorted(config()["channels"])


def describe() -> dict:
    """Safe to show: channel names and where they send, never a token."""
    cfg = config()
    return {"channels": {name: {"chat_id": c.get("chat_id", "")} for name, c in cfg["channels"].items()},
            "daily_summary": bool(cfg.get("daily_summary"))}


# ─── channels ────────────────────────────────────────────────────────────────

def _send_telegram(cfg: dict, text: str) -> None:
    try:
        r = httpx.post(f"https://api.telegram.org/bot{cfg['token']}/sendMessage",
                       json={"chat_id": cfg["chat_id"], "text": text, "disable_web_page_preview": True},
                       timeout=10)
    except httpx.HTTPError as exc:
        raise AlertError(f"could not reach Telegram ({type(exc).__name__})") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or not body.get("ok"):
        raise AlertError(str(body.get("description") or f"Telegram answered {r.status_code}")[:200])


CHANNELS: dict[str, Callable[[dict, str], None]] = {"telegram": _send_telegram}


# ─── sending ─────────────────────────────────────────────────────────────────

def clean(text: str) -> str:
    text = _SECRET_RE.sub("[removed]", " ".join(str(text).split()))
    return text[:DETAIL_MAX]


_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
REFUSAL_MAX = 160


def clean_refusal(text: str) -> str:
    """A refusal's text as it may go to the person: it carries the caller's own words (a kind, an address, a
    label), so no address and none of the 'how to allow it' advice a caller could plant; control characters
    out, short. The audit log has the full reason."""
    text = _CONTROL_RE.sub(" ", str(text))
    for marker in (" they add it", " add it with", "emporia signer policy"):
        text = text.split(marker, 1)[0]
    text = _ADDRESS_RE.sub("an address not on its list (see `emporia signer log`)", text)
    return clean(text)[:REFUSAL_MAX]


def _line(agent_id: str, event: str, detail: str) -> str:
    return f"Emporia signer · {agent_id} · {event}: {clean(clean_refusal(detail) if event == 'refused' else detail)}"


def _allow(agent_id: str, event: str = "refused") -> str | None:
    """The text to send, or None when over the hourly cap (one notice per hour). The cap is per agent AND
    per kind of alert: refusals, which a caller can make for free, cannot use up the room a payout-wallet,
    key or relay-list alert needs."""
    now = time.time()
    bucket = f"{agent_id}\x00{event}"
    with _LOCK:
        recent = [t for t in _SENT.get(bucket, []) if now - t < 3600]
        if len(recent) < MAX_PER_HOUR:
            recent.append(now)
            _SENT[bucket] = recent
            return ""
        _SENT[bucket] = recent
        if now - _CAPPED.get(bucket, 0) < 3600:
            return None
        _CAPPED[bucket] = now
        return f"Emporia signer · {agent_id} · more {event} alerts, see `emporia signer log {agent_id}`"


def _deliver(name: str, cfg: dict, text: str) -> tuple[bool, str]:
    send = CHANNELS.get(name)
    if send is None:
        return False, "unknown channel"
    try:
        send(cfg, text)
    except AlertError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - never let a channel bug reach signing
        return False, f"{type(exc).__name__}"
    return True, "sent"


def _record(agent_id: str, name: str, ok: bool, why: str) -> None:
    try:
        signer.audit({"op": "alert", "agent": agent_id, "channel": name,
                      "result": "sent" if ok else "error", **({} if ok else {"why": why[:200]})})
    except Exception:  # noqa: BLE001
        pass


def _run(agent_id: str, name: str, cfg: dict, text: str) -> tuple[bool, str]:
    ok, why = _deliver(name, cfg, text)
    _record(agent_id, name, ok, why)
    return ok, why


def _prepare(agent_id: str, event: str, detail: str) -> tuple[str | None, dict]:
    if event not in EVENTS:
        raise ValueError(f"unknown alert event {event!r}")
    chans = config()["channels"]
    if not chans:
        return None, {}
    text = _line(agent_id, event, detail)
    if event != "test":
        capped = _allow(agent_id, event)
        if capped is None:
            return None, {}
        if capped:
            text = capped
    return text, chans


def notify_sync(agent_id: str, event: str, detail: str) -> list[tuple[str, bool, str]]:
    """Send now, inline; one (channel, ok, why) per configured channel."""
    text, chans = _prepare(agent_id, event, detail)
    if text is None:
        return []
    return [(name, *_run(agent_id, name, cfg, text)) for name, cfg in chans.items()]


def notify(agent_id: str, event: str, detail: str) -> None:
    """Send in the background; never raises, never waits for the network."""
    try:
        text, chans = _prepare(agent_id, event, detail)
        for name, cfg in (chans if text else {}).items():
            threading.Thread(target=_run, args=(agent_id, name, cfg, text), daemon=True).start()
    except Exception:  # noqa: BLE001
        pass
