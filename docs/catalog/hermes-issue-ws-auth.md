# Draft issue for hermes-agent: a public WebSocket credential check for dashboard plugin APIs

*Draft, not filed. To open on the hermes-agent repository when the owner decides.*

**Title:** Dashboard plugins: a public helper to authorize a WebSocket upgrade on a plugin route

**Problem**

A dashboard plugin's `plugin_api.py` router is mounted under `/api/plugins/<name>/`.
- Hermes authenticates every **HTTP** request there (its `/api/` gate).
- It leaves a **WebSocket** upgrade to the route. `PluginApiWebSocketGate` refuses a disabled
  plugin before routing, but it does not check the viewer's credential. A ticket is single-use,
  and only the route may redeem it.

So a plugin with a socket route has to check the credential itself, and the only function that
does it the way core does is private: `hermes_cli.web_server_chat._ws_auth_ok(ws)`. It accepts
`?token=`, `?ticket=`, `?internal=` and the native grant.

`hermes plugins validate` correctly warns on that import (rule 9, private core import). The
bundled kanban plugin uses the same function (`plugins/kanban/dashboard/plugin_api.py`,
`_ws_upgrade_authorized`), with the comment "so this endpoint can never drift from core auth".
Re-implementing the check from `hermes_cli.dashboard_auth.ws_tickets` pieces is the drift that
comment warns against.

**Proposal**

A documented helper on the public surface:

```python
from hermes_cli.dashboard_auth import authorize_plugin_websocket

@router.websocket("/ws/{path:path}")
async def ws(socket: WebSocket, path: str):
    if not authorize_plugin_websocket(socket):     # same rules as core's own sockets
        await socket.close(code=1008)
        return
    ...
```

- It wraps `_ws_auth_ok`, so a plugin never imports `web_server_chat`.
- It is added to the developer guide's dashboard-plugin page, and so to the validator's
  public-surface list.
- An alternative with the same effect: `PluginApiWebSocketGate` checks the credential for routes
  a plugin marks as needing it (for example a `websocket_auth = "core"` manifest key), so the
  route never sees an unauthenticated socket.

**Who needs it**

- The bundled kanban plugin.
- Emporia (catalog submission), whose dashboard tab proxies its local console's sockets.
- Any plugin with live updates.

Until it exists, Emporia keeps the private import, mirroring kanban, and fails closed: if the
import fails, the socket is refused (`dashboard/plugin_api.py`).
