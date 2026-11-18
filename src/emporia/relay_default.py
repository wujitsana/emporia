"""The relay URL an agent or the CLI uses: the person's setting, never a silent guess.

`EMPORIA_RELAY_URL` names the relay. When it is empty, or a reference the host did not fill in (`${EMPORIA_RELAY_URL}`),
nothing falls back to `http://localhost:8088` (whatever listens there is not necessarily the relay the person
chose): the caller refuses with `MESSAGE`. The one exception is an explicit development setting,
`EMPORIA_DEV_LOCAL_RELAY=1`, which makes the old local default the answer again (for a local relay under test).
"""

from __future__ import annotations

import os

DEV_FLAG = "EMPORIA_DEV_LOCAL_RELAY"
LOCAL = "http://localhost:8088"
MESSAGE = ("no relay is configured: EMPORIA_RELAY_URL is empty or was not handed to this process. Tell your person: "
           "run `emporia agent refresh NAME` (or `hermes emporia setup`) to set it, or pass the relay's URL "
           "(for a relay under development only: set EMPORIA_DEV_LOCAL_RELAY=1)")


class NoRelay(RuntimeError):
    """No relay URL is configured (and the development setting is off)."""

    def __init__(self, message: str = MESSAGE):
        super().__init__(message)


def usable(value: str | None) -> str:
    """The setting if it names a relay: not empty, not an unresolved `${...}` reference."""
    text = (value or "").strip()
    return "" if not text or "${" in text else text


def dev_local() -> bool:
    return os.getenv(DEV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def resolve(value: str | None) -> str:
    """The relay URL from a setting's value: that value, else the local default when the development setting is on,
    else "" (no relay)."""
    return usable(value) or (LOCAL if dev_local() else "")
