"""Shared logging + error-tracking setup for the relay and the MCP server.

Both processes need the same thing, configured the same way, but from different
entry points: `relay_server.py`'s `emporia-relay` console script calls `main()`,
but `python -m uvicorn emporia.relay_server:app` (the documented local-dev path,
`scripts/local_relay.py`) imports the module and serves `app` directly, never
calling `main()` at all. So `configure_logging()`/`configure_sentry()` are called
at *module import time* in both `relay_server.py` and `mcp_server.py`, not from
`main()` — the only way to cover every real invocation path.

`mcp_server.py` runs as an MCP **stdio** server by default — stdout *is* the
protocol channel there. Everything here defaults to stderr, which is already
safe for that process and is captured by Docker (or any log driver) exactly as
well as stdout for the relay's own HTTP process.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_LOGGER_NAME = "emporia"
_configured = False
_sentry_configured = False

# Every attribute a stdlib LogRecord carries before any `extra=` is applied —
# used to find the caller-supplied extra fields without hardcoding their names.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord(
    "", 0, "", 0, "", (), None,
).__dict__.keys()) | {"message", "asctime"}


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts/level/logger/msg/request_id, plus whatever
    `extra={...}` the call site supplied. No dependency — this is the whole
    formatter."""

    def format(self, record: logging.LogRecord) -> str:
        # Not formatTime() -- its strftime backend doesn't support %f
        # (microseconds), a datetime-only format code.
        ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and key != "request_id":
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Idempotent — safe to call from every module that needs it (relay,
    mcp_server, and re-imports under the test suite) without double-attaching
    handlers."""
    global _configured
    if _configured:
        return
    _configured = True

    level_name = os.getenv("EMPORIA_LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level = logging.INFO

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_RequestIdFilter())

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.addHandler(handler)
    # Not the whole process's log messages -- fastapi/uvicorn/etc keep their own
    # handlers. Scoped to the "emporia" namespace only, and doesn't propagate to
    # the root logger, so nothing gets double-printed.
    logger.propagate = False


def configure_sentry() -> None:
    """No-op unless SENTRY_DSN is set, matching this codebase's existing
    convention for optional external services (e.g.
    EMPORIA_NEMO_GUARDRAILS_ENABLED) -- present in the dependency list, inert
    until an operator opts in."""
    global _sentry_configured
    if _sentry_configured:
        return
    _sentry_configured = True

    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", "development"),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
    )
