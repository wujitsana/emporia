"""Emporia's Hermes dashboard tab (P126) — everything is in dashboard/.

A dashboard-only plugin: Hermes's plugin discovery and `hermes plugins enable`
need this package to recognise it, and the dashboard's consent rule needs it
enabled before dashboard/plugin_api.py may run. It registers nothing with the
agent: no tools, no hooks.
"""


def register(ctx) -> None:  # noqa: ARG001 — nothing to register
    return None
