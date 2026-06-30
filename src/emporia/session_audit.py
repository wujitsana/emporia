"""Dual-track hash-chained session audit log for Emporia.

Two tracks per session:
  private/  — full event log, not shared; never leaves this node
  public/   — SHA-256 hash-chained receipt log for dispute resolution

Public log chain: each entry hashes (prev_hash + sender + action + payload + signature)
so tampering with any entry invalidates all subsequent hashes.

Replaces the PTGS triple-write pattern (two DBs + daily JSONL) with a clean two-track
pattern. Use for per-session auditability, not as the primary message store.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

_AUDIT_BASE = Path(os.getenv("EMPORIA_AUDIT_DIR", "~/.hermes/emporia_audit")).expanduser()
_PRIVATE_DIR = _AUDIT_BASE / "private"
_PUBLIC_DIR = _AUDIT_BASE / "public"


def _ensure_dirs() -> None:
    _PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    _PUBLIC_DIR.mkdir(parents=True, exist_ok=True)


def open_dual_session(session_id: str, peer_agent_id: str, topic: str) -> None:
    """Initialize both audit log files for a new session."""
    _ensure_dirs()
    private_path = _PRIVATE_DIR / f"{session_id}.jsonl"
    if not private_path.exists():
        private_path.write_text("")

    public_path = _PUBLIC_DIR / f"{session_id}.jsonl"
    if not public_path.exists():
        genesis = {
            "session_id": session_id,
            "peer": peer_agent_id,
            "topic": topic,
            "genesis": True,
            "block_hash": "GENESIS",
            "timestamp": int(time.time()),
        }
        public_path.write_text(json.dumps(genesis) + "\n")


def log_private_event(session_id: str, sender: str, event_type: str, content: Any) -> None:
    """Append a private event line (not shared)."""
    _ensure_dirs()
    path = _PRIVATE_DIR / f"{session_id}.jsonl"
    entry = {
        "timestamp": int(time.time()),
        "sender": sender,
        "event_type": event_type,
        "content": content,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def log_public_receipt(
    session_id: str,
    sender: str,
    action: str,
    payload: dict[str, Any],
    signature: str,
) -> str:
    """Append a hash-chained public receipt. Returns the new block_hash."""
    _ensure_dirs()
    path = _PUBLIC_DIR / f"{session_id}.jsonl"

    # Read last block hash
    prev_hash = "GENESIS"
    if path.exists():
        lines = path.read_text().strip().split("\n")
        for line in reversed(lines):
            if line.strip():
                try:
                    last = json.loads(line)
                    prev_hash = last.get("block_hash", "GENESIS")
                    break
                except json.JSONDecodeError:
                    pass

    serialized_frame = f"{prev_hash}:{sender}:{action}:{json.dumps(payload, sort_keys=True)}:{signature}"
    block_hash = hashlib.sha256(serialized_frame.encode("utf-8")).hexdigest()

    entry = {
        "timestamp": int(time.time()),
        "sender": sender,
        "action": action,
        "payload": payload,
        "signature": signature,
        "parent_block_hash": prev_hash,
        "block_hash": block_hash,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return block_hash


def get_public_log(session_id: str) -> list[dict[str, Any]]:
    """Return all public receipt entries for a session."""
    path = _PUBLIC_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def verify_chain(session_id: str) -> tuple[bool, str]:
    """Verify the hash chain integrity of the public receipt log.

    Returns (ok, message).
    """
    entries = get_public_log(session_id)
    if not entries:
        return True, "empty log"

    prev_hash = "GENESIS"
    for i, entry in enumerate(entries):
        if entry.get("genesis"):
            prev_hash = "GENESIS"
            continue
        sender = entry.get("sender", "")
        action = entry.get("action", "")
        payload = entry.get("payload", {})
        signature = entry.get("signature", "")
        expected_frame = f"{prev_hash}:{sender}:{action}:{json.dumps(payload, sort_keys=True)}:{signature}"
        expected_hash = hashlib.sha256(expected_frame.encode("utf-8")).hexdigest()
        if entry.get("block_hash") != expected_hash:
            return False, f"hash mismatch at entry {i}: expected {expected_hash}, got {entry.get('block_hash')}"
        prev_hash = entry["block_hash"]

    return True, f"chain valid ({len(entries)} entries)"
