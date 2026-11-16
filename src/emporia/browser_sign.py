"""A one-shot local page for signing the ERC-8004 link statement with a browser wallet.

Runs only while `emporia agent link-8004 NAME --browser` waits, and never inside the dashboard (which has no wallet UI).
The standard library only: `http.server` on 127.0.0.1 at a random port, one page, one POST.

* The address carries a one-time token (`/<token>`). Any other path, or a wrong token, is a plain 404; the Host
  header must be this server's own address (so a rebinding page cannot reach it) and a POST's Origin, when sent, must
  be this page's.
* It accepts one well-formed signature, answers it, and stops. It also stops after `TIMEOUT` seconds, or on Ctrl-C.
  A malformed POST is refused and does not use the one.
* Strict CSP: nothing loads from anywhere; the one script and one style are inline, by nonce; the page may only
  talk to its own origin. `nosniff`, no referrer, no caching, no framing.
* The page asks the wallet for the account, checks the wallet's chain against the link's and, if they differ, asks the
  wallet to switch (`wallet_switchEthereumChain`) and says so when the wallet refuses; then one `eth_signTypedData_v4`.
  Wallets are found through EIP-6963 announcements, falling back to `window.ethereum`. No SDK, no remote script.

The page never decides anything: the CLI verifies the returned signature exactly as it does one given with
`--signature`, and only then records it.
"""
from __future__ import annotations

import html
import json
import re
import secrets
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any

TIMEOUT = 600.0                      # seconds the page stays up
MAX_BODY = 16 * 1024
MAX_SIG_BYTES = 4096
HOST = "127.0.0.1"
_ADDR = re.compile(r"0x[0-9a-fA-F]{40}")
_HEX = re.compile(r"(0x)?[0-9a-fA-F]+")


class BrowserSignError(Exception):
    """Said to the person as it is."""


def _json_for_html(value: Any) -> str:
    """JSON that is safe inside a <script> element: no `<`, `>`, `&` or line separators."""
    return (json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1a1a1a;--dim:#666;--line:#ccc;--bad:#b00020;--good:#0a6b2d}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--dim:#aaa;--line:#444;--bad:#ff8a80;--good:#7ddc9a}}
body{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;margin:0 auto;padding:24px 16px;max-width:42rem}
h1{font-size:1.25rem;margin:0 0 .5rem}p{margin:.5rem 0}.dim{color:var(--dim)}
pre{background:transparent;border:1px solid var(--line);padding:12px;overflow:auto;font-size:.8rem;white-space:pre-wrap;word-break:break-all}
button,select{font:inherit;padding:.5rem 1rem;margin:.25rem 0}#status{min-height:1.5rem}.bad{color:var(--bad)}.good{color:var(--good)}
"""

SCRIPT = r"""
(() => {
  const doc = JSON.parse(document.getElementById("page-data").textContent);
  const td = doc.typed_data, wantChain = Number(td.domain.chainId), signer = String(td.message.signer).toLowerCase();
  const $ = (id) => document.getElementById(id);
  const say = (text, cls) => { const s = $("status"); s.textContent = text; s.className = cls || ""; };
  const found = [];
  const add = (info, provider) => {
    if (!provider || found.some((f) => f.provider === provider)) return;
    found.push({ name: (info && info.name) || "your browser wallet", provider });
    render();
  };
  function render() {
    const pick = $("wallet");
    pick.textContent = "";
    found.forEach((f, i) => { const o = document.createElement("option"); o.value = String(i); o.textContent = f.name; pick.appendChild(o); });
    pick.hidden = found.length < 2;
    $("sign").disabled = found.length === 0;
    $("nowallet").hidden = found.length > 0;
  }
  window.addEventListener("eip6963:announceProvider", (e) => { if (e.detail) add(e.detail.info, e.detail.provider); });
  window.dispatchEvent(new Event("eip6963:requestProvider"));
  window.addEventListener("load", () => { setTimeout(() => { if (!found.length && window.ethereum) add(null, window.ethereum); render(); }, 300); });
  const hexChain = (n) => "0x" + n.toString(16);
  const refused = (e) => e && (e.code === 4001 || e.code === "ACTION_REJECTED");
  async function chainId(p) { return parseInt(await p.request({ method: "eth_chainId" }), 16); }
  async function go() {
    $("sign").disabled = true;
    const chosen = found[Number($("wallet").value || 0)];
    if (!chosen) { say("No wallet found in this browser.", "bad"); return; }
    const p = chosen.provider;
    try {
      say("Asking your wallet for the account...");
      const accounts = (await p.request({ method: "eth_requestAccounts" })).map((a) => String(a).toLowerCase());
      if (!accounts.includes(signer)) {
        say("Your wallet did not offer the account " + td.message.signer + ". Select that account in your wallet, then press Sign again.", "bad");
        $("sign").disabled = false; return;
      }
      if (await chainId(p) !== wantChain) {
        say("Your wallet is on another chain. Asking it to switch to " + doc.chain_label + "...");
        try {
          await p.request({ method: "wallet_switchEthereumChain", params: [{ chainId: hexChain(wantChain) }] });
        } catch (e) {
          say("Your wallet did not switch to " + doc.chain_label + " (chain id " + wantChain + ")" +
              (refused(e) ? ": you declined" : e && e.code === 4902 ? ": it does not know that chain" : "") +
              ". Switch it yourself, then press Sign again.", "bad");
          $("sign").disabled = false; return;
        }
        if (await chainId(p) !== wantChain) {
          say("Your wallet is still not on " + doc.chain_label + ". Switch it yourself, then press Sign again.", "bad");
          $("sign").disabled = false; return;
        }
      }
      say("Waiting for your signature in the wallet...");
      const signature = await p.request({ method: "eth_signTypedData_v4", params: [td.message.signer, JSON.stringify(td)] });
      const r = await fetch(location.pathname.replace(/\/$/, "") + "/sign", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ signature, account: td.message.signer }),
      });
      if (!r.ok) { say("The terminal did not accept it: " + (await r.text()), "bad"); $("sign").disabled = false; return; }
      say("Signed. The terminal has it: you can close this tab.", "good");
    } catch (e) {
      say(refused(e) ? "You declined to sign. Nothing was sent. Press Sign to try again." : "The wallet said: " + ((e && e.message) || e), "bad");
      $("sign").disabled = false;
    }
  }
  $("sign").addEventListener("click", go);
  render();
})();
"""


def page_html(typed_data: dict, *, chain_label: str, agent_name: str, nonce: str) -> str:
    """The page. `typed_data` is the JSON form a wallet takes (`erc8004.typed_data_json`)."""
    msg = typed_data["message"]
    data = _json_for_html({"typed_data": typed_data, "chain_label": chain_label})
    shown = html.escape(json.dumps({k: typed_data[k] for k in ("domain", "primaryType", "message")}, indent=2))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Sign the link</title>
<style nonce="{nonce}">{STYLE}</style></head><body>
<h1>Link ERC-8004 agent #{html.escape(str(msg['agentId']))} to {html.escape(agent_name)}</h1>
<p>Signing links ERC-8004 agent #{html.escape(str(msg['agentId']))} on {html.escape(chain_label)} to the Emporia agent
{html.escape(agent_name)}. It moves no funds and grants no permission: nothing goes on-chain and it costs no gas.</p>
<p class="dim">Signer: <code>{html.escape(str(msg['signer']))}</code></p>
<p><select id="wallet" hidden aria-label="wallet"></select> <button id="sign" type="button" disabled>Sign</button></p>
<p id="status" role="status"></p>
<p id="nowallet" class="dim" hidden>No wallet was found in this browser. Open this page where your browser wallet is, or sign
the message below with the signer's wallet some other way and give it to the terminal with <code>--signature</code>.</p>
<p class="dim">What you are asked to sign (EIP-712 typed data, domain &ldquo;Emporia agent link&rdquo;):</p>
<pre>{shown}</pre>
<script type="application/json" id="page-data">{data}</script>
<script nonce="{nonce}">{SCRIPT}</script>
</body></html>
"""


def csp(nonce: str) -> str:
    return (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


CONN_TIMEOUT = 5.0                  # seconds a connection may sit silent before it is dropped


class SigningPage:
    """One page, one signature. `start()` binds; `wait()` serves until a signature arrives or the time is up."""

    def __init__(self, typed_data: dict, *, chain_label: str, agent_name: str, token: str | None = None,
                 timeout: float = TIMEOUT):
        self.typed_data, self.chain_label, self.agent_name = typed_data, chain_label, agent_name
        self.token = token or secrets.token_urlsafe(24)
        self.timeout = timeout
        self.result: dict | None = None
        self._server: HTTPServer | None = None
        self._lock = threading.Lock()
        self._signer = str(typed_data["message"]["signer"]).lower()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/{self.token}"

    def start(self) -> SigningPage:
        page = self

        class Handler(BaseHTTPRequestHandler):
            timeout = CONN_TIMEOUT      # a silent or slow connection is dropped; it never holds the page

            protocol_version = "HTTP/1.0"
            server_version = "emporia-sign"
            sys_version = ""

            def log_message(self, *args: Any) -> None:       # quiet: the terminal is the person's
                pass

            def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8", nonce: str = "") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy", csp(nonce) if nonce else "default-src 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _own_host(self) -> bool:
                return self.headers.get("Host", "") == f"{HOST}:{page.port}"

            def _route(self) -> str:
                """'page' | 'sign' | '' for this path: only the right token gets either."""
                path = self.path.split("?", 1)[0]
                for name, suffix in (("page", ""), ("page", "/"), ("sign", "/sign")):
                    want = f"/{page.token}{suffix}"
                    if len(path) == len(want) and secrets.compare_digest(path.encode(), want.encode()):
                        return name
                return ""

            def do_GET(self) -> None:
                if not self._own_host() or self._route() != "page":
                    return self._send(404, b"not found")
                nonce = secrets.token_urlsafe(16)
                body = page_html(page.typed_data, chain_label=page.chain_label, agent_name=page.agent_name,
                                 nonce=nonce).encode()
                self._send(200, body, "text/html; charset=utf-8", nonce)

            do_HEAD = do_GET

            def do_POST(self) -> None:
                if not self._own_host() or self._route() != "sign":
                    return self._send(404, b"not found")
                origin = self.headers.get("Origin")
                if origin is not None and origin != f"http://{HOST}:{page.port}":
                    return self._send(403, b"wrong origin")
                if page.result is not None:
                    return self._send(409, b"a signature was already received")
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    return self._send(415, b"send JSON")
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    return self._send(411, b"length required")
                if not 0 < length <= MAX_BODY:
                    return self._send(413, b"too large")
                try:
                    body = json.loads(self.rfile.read(length))
                    sig, account = body["signature"], body["account"]
                except (ValueError, KeyError, TypeError):
                    return self._send(400, b"malformed")
                if not isinstance(sig, str) or not isinstance(account, str) or not _HEX.fullmatch(sig) \
                        or len(sig.removeprefix("0x")) % 2 or len(sig.removeprefix("0x")) > 2 * MAX_SIG_BYTES \
                        or not _ADDR.fullmatch(account) or account.lower() != page._signer:
                    return self._send(400, b"not a signature for the signer")
                with page._lock:
                    if page.result is not None:
                        return self._send(409, b"a signature was already received")
                    page.result = {"signature": sig if sig.startswith("0x") else "0x" + sig, "account": account}
                self._send(200, b'{"ok":true}', "application/json")

        # One thread per connection (daemon, never joined), so an idle browser pre-connection cannot hold the
        # accept loop past the time limit; each connection's socket times out on its own (CONN_TIMEOUT).
        self._server = ThreadingHTTPServer((HOST, 0), Handler)
        self._server.daemon_threads = True
        self._server.timeout = 0.2
        return self

    def wait(self) -> dict:
        """Serve until one signature arrives (returned) or `timeout` passes (BrowserSignError). Always closes."""
        assert self._server is not None, "start() first"
        began = time.monotonic()
        try:
            while self.result is None:
                if time.monotonic() - began >= self.timeout:
                    raise BrowserSignError(f"no signature arrived in {int(self.timeout // 60) or 1} minutes: the page is closed")
                self._server.handle_request()
            return self.result
        finally:
            self.close()

    def close(self) -> None:
        if self._server is not None:
            self._server.server_close()
