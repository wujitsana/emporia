"""HTTP routers, split out of `relay_server` one domain at a time (P56).

`relay_server.py` was 10,525 lines — 46% of the package — carrying 91 route
handlers, the schema, the migrations, the auth helpers and both background
loops. This package is where the handlers go.

**The convention every router follows, and why it is not the obvious one.**
A router reaches shared relay state through the *module object*:

    from emporia import relay_server as rs
    ...
    with rs.DB_LOCK, rs.get_db() as conn:

not `from emporia.relay_server import DB_LOCK, get_db`. The difference is when
the name resolves. A module-scope `from ... import` binds the value once at
import; `rs.NAME` looks it up on every call. The suite has 43 sites that patch
`emporia.relay_server.<name>` — `_assert_payload_safe_counted`, `TEMPO_ENABLED`,
`record_payment`, `_fetch_federation_batch` and more — and every one of those
patches replaces an attribute on the module object. Bind early and the router
holds the original, the patch does nothing, and the test passes for the wrong
reason. This is the same hazard `relay_server` already documents for its own
function-local `from emporia.payments import ...` lines.

Routers are imported and included at the *end* of `relay_server`, after every
name they reference exists. The circular import is deliberate and safe because
nothing is dereferenced at import time — only inside handlers.
"""
