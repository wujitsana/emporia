"""Emporia's side of the Hermes platform adapter.

The adapter Hermes loads lives in `hermes_plugin/emporia/` at the repo root and
is copied into a profile's `plugins/platforms/` by the installer. It imports
nothing from here: it runs `bridge.py` in Emporia's own environment and reads
notices from it, one JSON line each.

- `tunnel.py`  — the authenticated, reconnecting WebSocket to the relay.
- `frames.py`  — what each relay frame says to the agent, and who it is from.
- `bridge.py`  — tunnel in, JSON lines out; the process the adapter runs.
"""
