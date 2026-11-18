"""The agent's own file of sealed values (moves, nonces, rand secrets), read and written under a lock.

Shared by the MCP server and the Python SDK so both keep one format in one place: a JSON dict in a 0600 file next to
the agent's keys. Nothing here talks to a relay.
"""

from __future__ import annotations

import json
from typing import Any


def write_commitments(path, saved: dict) -> None:
    from emporia.identity import _write_secret_atomically
    _write_secret_atomically(path, json.dumps(saved).encode())


class Commitments:
    """Review follow-up M5: the round entries of `path`, read and written under an
    exclusive lock (MCP calls can run at once). Yields the dict; writes it back on
    leaving, if it changed."""

    def __init__(self, path):
        from emporia.identity import _locked
        self.path, self.lock = path, _locked(path.with_name(f".{path.name}.lock"))

    def __enter__(self) -> dict:
        self.lock.__enter__()
        try:
            self.saved = json.loads(self.path.read_text()) if self.path.exists() else {}
        except BaseException:
            self.lock.__exit__(None, None, None)
            raise
        self.before = json.dumps(self.saved, sort_keys=True)
        return self.saved

    def __exit__(self, *exc: Any) -> None:
        try:
            if exc[0] is None and json.dumps(self.saved, sort_keys=True) != self.before:
                write_commitments(self.path, self.saved)
        finally:
            self.lock.__exit__(*exc)
