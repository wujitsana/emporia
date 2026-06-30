"""Emporia platform plugin for Hermes.

Wires the Emporia relay into the Hermes gateway as a platform, alongside
Telegram and Discord. Agents receive challenges, game updates, and negotiation
messages through this platform without needing inbound ports.

The gateway auto-loads this plugin from plugins/platforms/emporia/ on startup
or after /reload-mcp.

Entry point: EmporiaAdapter (defined in adapter.py).
"""

from emporia.plugins.platforms.emporia.adapter import EmporiaAdapter

__all__ = ["EmporiaAdapter"]
