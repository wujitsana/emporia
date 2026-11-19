"""A one-shot local page for signing the ERC-8004 link statement (or sending the registration transaction) with a browser wallet.

Runs only while `emporia agent link-8004 NAME --browser` (or `register-8004 NAME --browser`) waits, and never inside the dashboard (which has no wallet UI).
The standard library only: `http.server`, one page, one POST. It listens on loopback at a random port by default;
`--port` fixes the port (for an SSH tunnel) and `--listen` picks the address: inside a container the page listens on the
container's own private address so the host can tunnel to it. It never listens on a public interface (`resolve_listen`).

* The address carries a one-time token (`/<token>`). Any other path, or a wrong token, is a plain 404; the Host
  header must be `127.0.0.1:PORT` or `localhost:PORT` (`[::1]:PORT` for an IPv6 loopback listener): the address the
  browser uses through a tunnel, never another name (so a rebinding page cannot reach it); a POST's Origin, when sent,
  must be one of those origins.
* It accepts one well-formed signature, answers it, and stops. It also stops after `TIMEOUT` seconds, or on Ctrl-C.
  A malformed POST is refused and does not use the one.
* Strict CSP: nothing loads from anywhere; the one script and one style are inline, by nonce; the page may only
  talk to its own origin. `nosniff`, no referrer, no caching, no framing.
* The page asks the wallet for the account, checks the wallet's chain against the link's and, if they differ, asks the
  wallet to switch (`wallet_switchEthereumChain`); a wallet that does not know the chain (error 4902) is offered it
  through `wallet_addEthereumChain` with Emporia's public parameters for that chain, then asked to switch again; then one `eth_signTypedData_v4`.
  Wallets are found through EIP-6963 announcements, falling back to `window.ethereum`. No SDK, no remote script.

The page never decides anything: the CLI verifies the returned signature exactly as it does one given with
`--signature`, and only then records it.
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import secrets
import socket
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TIMEOUT = 600.0                      # seconds the page stays up
MAX_BODY = 16 * 1024
MAX_SIG_BYTES = 4096
HOST = "127.0.0.1"
_ADDR = re.compile(r"0x[0-9a-fA-F]{40}")
_HEX = re.compile(r"(0x)?[0-9a-fA-F]+")
_TX = re.compile(r"0x[0-9a-fA-F]{64}")


class BrowserSignError(Exception):
    """Said to the person as it is."""


# -- where the page listens, and how the person reaches it ------------------------------------------------------
PORT_MIN, PORT_MAX = 1024, 65535
_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "fc00::/7", "fe80::/10"))


def check_port(port: int | str | None) -> int:
    """0 (a random port) or a port in 1024-65535; BrowserSignError otherwise."""
    try:
        n = int(port or 0)
    except (TypeError, ValueError):
        raise BrowserSignError(f"--port must be a number from {PORT_MIN} to {PORT_MAX}") from None
    if n != 0 and not PORT_MIN <= n <= PORT_MAX:
        raise BrowserSignError(f"--port must be from {PORT_MIN} to {PORT_MAX} (got {n})")
    return n


def validate_listen(addr: str) -> str:
    """The address, if the page may listen on it: loopback, private (RFC 1918, 100.64/10, fc00::/7) or link-local.
    Everything else (0.0.0.0, ::, a public address, a name) is refused: the page never listens on a public interface."""
    text = str(addr or "").strip()
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise BrowserSignError(f"--listen takes an IP address (or auto), not {text!r}") from None
    if ip.is_unspecified:
        raise BrowserSignError("--listen refuses 0.0.0.0 and ::: they listen on every interface, public ones included. "
                               "Give one private address, or leave it on auto")
    if not (ip.is_loopback or any(ip in net for net in _PRIVATE_NETS)):
        raise BrowserSignError(f"--listen refuses {text}: it is a public address, and this page must never listen on "
                               "one. Use auto, 127.0.0.1, or a private address (10.x, 172.16-31.x, 192.168.x)")
    return str(ip)


def in_container(root: str | Path = "/", env: dict[str, str] | None = None) -> bool:
    """True inside a container: `/.dockerenv` or `/run/.containerenv`, a container word in `/proc/1/cgroup` or
    `/proc/self/mountinfo`, or a `container` variable in the environment. `root` and `env` are for tests."""
    root = Path(root)
    env = os.environ if env is None else env
    if env.get("container"):
        return True
    if (root / ".dockerenv").exists() or (root / "run/.containerenv").exists():
        return True
    for rel in ("proc/1/cgroup", "proc/self/mountinfo"):
        try:
            text = (root / rel).read_text(errors="replace")
        except OSError:
            continue
        if re.search(r"docker|containerd|kubepods|libpod|lxc", text):
            return True
    return False


def _default_gateway(route_file: str | Path = "/proc/net/route") -> str:
    """The default gateway's IPv4 address from the kernel's route table ('' when there is none)."""
    try:
        lines = Path(route_file).read_text().splitlines()[1:]
    except OSError:
        return ""
    for line in lines:
        f = line.split()
        try:
            if len(f) >= 4 and f[1] == "00000000" and int(f[3], 16) & 2:
                return socket.inet_ntoa(bytes.fromhex(f[2])[::-1])
        except (ValueError, OSError):
            continue
    return ""


def container_ip(route_file: str | Path = "/proc/net/route") -> str:
    """This container's own private IPv4: the source address the kernel picks for the default gateway. A UDP connect
    sends nothing. '' when it cannot be found."""
    target = _default_gateway(route_file) or "192.0.2.1"       # TEST-NET-1: only the route is looked up
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def resolve_listen(listen: str = "auto", *, container: bool | None = None) -> str:
    """`auto` -> 127.0.0.1, or inside a container the container's private address; else the validated address."""
    want = str(listen or "auto").strip().lower()
    if want != "auto":
        return validate_listen(want)
    if container is None:
        container = in_container()
    if container:
        try:
            return validate_listen(container_ip())
        except BrowserSignError:
            pass                       # no private address found: loopback (a published port will not reach it)
    return HOST


def ssh_session(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("SSH_CONNECTION") or env.get("SSH_CLIENT"))


def page_url(listen: str, port: int, token: str) -> str:
    """What the browser opens: always the loopback address on this port (through a tunnel it is the local end)."""
    return f"http://{'[::1]' if listen == '::1' else HOST}:{port}/{token}"


def instructions(port: int, listen: str, *, container: bool, ssh: bool, hostname: str = "", token: str = "",
                 chain_label: str = "", registry: str = "", what: str = "signature") -> tuple[str, bool]:
    """What to tell the person when the page starts, for the setup detected, and whether this machine opens the browser
    (only a local, non-container session does). The browser-side URL is always http://127.0.0.1:PORT/<token>, so a
    tunnel's local port must equal PORT."""
    url = page_url(listen, port, token)
    target = "127.0.0.1" if listen in ("127.0.0.1", "::1") else listen
    tunnel = f"ssh -L {port}:{target}:{port} <you>@{hostname or '<this-host>'}"
    ssh_text = (f"  Your browser is on another machine. On THAT machine, in a new terminal, run:\n\n"
                f"    {tunnel}\n\n"
                f"  (replace <you> with your login on this host; keep that terminal open). The local port must be {port},\n"
                f"  the same number: the page only answers at 127.0.0.1:{port}. Then open this in your browser:\n\n"
                f"    {url}\n")
    desktop_text = (f"\n  Docker Desktop (macOS/Windows) cannot reach a container's own address. There, publish the "
                    f"port on the host's\n  loopback only and open the URL on that machine. In compose:\n\n"
                    f"    ports: [\"127.0.0.1:{port}:{port}\"]\n\n"
                    f"  (or docker run -p 127.0.0.1:{port}:{port}); run this command with --port {port}.\n  Never publish it on 0.0.0.0. Then open:\n\n"
                    f"    {url}\n")
    if ssh:
        text = ssh_text
    elif container:
        text = ssh_text + desktop_text
    else:
        text = f"  Opening your browser at this address (it works on this machine only):\n\n    {url}\n"
    check = f"  Check the wallet popup: network {chain_label}" if chain_label else "  Check the wallet popup"
    if registry:
        check += f", and the 'to' address {registry}"
    check += f". This page only asks for this one {what} and closes.\n"
    return text + "\n" + check, not (ssh or container)


def chain_params(chain_id: int) -> dict | None:
    """`wallet_addEthereumChain` parameters from Emporia's own network list, for a chain it knows (else None)."""
    from emporia.networks import NETWORKS
    n = NETWORKS.get(f"eip155:{chain_id}")
    if n is None or n.family != "evm" or not n.rpc:
        return None
    out: dict[str, Any] = {"chainId": hex(chain_id), "chainName": n.label,
                           "nativeCurrency": {"name": "Ether", "symbol": "ETH", "decimals": 18}, "rpcUrls": [n.rpc]}
    base = n.explorer.removesuffix("tx/").rstrip("/") if n.explorer else ""
    if base:
        out["blockExplorerUrls"] = [base]
    return out


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
  const td = doc.typed_data, tx = doc.tx;
  const wantChain = Number(td ? td.domain.chainId : tx.chain_id), signer = td ? String(td.message.signer).toLowerCase() : "";
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
  // Make the wallet's chain the page's: false (and said why) when it is not.
  async function ensureChain(p) {
    if (await chainId(p) === wantChain) return true;
    say("Your wallet is on another chain. Asking it to switch to " + doc.chain_label + "...");
    const sw = () => p.request({ method: "wallet_switchEthereumChain", params: [{ chainId: hexChain(wantChain) }] });
    try {
      try {
        await sw();
      } catch (e) {
        if (!(e && e.code === 4902 && doc.add_chain)) throw e;
        // The wallet does not know the chain: offer it Emporia's public parameters for it, then switch again.
        say("Your wallet does not know " + doc.chain_label + ". Asking it to add the network (public settings: " +
            doc.add_chain.rpcUrls[0] + ")...");
        await p.request({ method: "wallet_addEthereumChain", params: [doc.add_chain] });
        if (await chainId(p) !== wantChain) await sw();
      }
    } catch (e) {
      say("Your wallet did not switch to " + doc.chain_label + " (chain id " + wantChain + ")" +
          (refused(e) ? ": you declined" : e && e.code === 4902 ? ": it does not know that chain" : "") +
          ". Switch it yourself, then press the button again.", "bad");
      return false;
    }
    if (await chainId(p) !== wantChain) {
      say("Your wallet is still not on " + doc.chain_label + ". Switch it yourself, then press the button again.", "bad");
      return false;
    }
    return true;
  }
  async function post(body) {
    const r = await fetch(location.pathname.replace(/\/$/, "") + "/sign", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok) { say("The terminal did not accept it: " + (await r.text()), "bad"); $("sign").disabled = false; return false; }
    return true;
  }
  async function go() {
    $("sign").disabled = true;
    const chosen = found[Number($("wallet").value || 0)];
    if (!chosen) { say("No wallet found in this browser.", "bad"); return; }
    const p = chosen.provider;
    try {
      say("Asking your wallet for the account...");
      const accounts = (await p.request({ method: "eth_requestAccounts" })).map((a) => String(a).toLowerCase());
      if (td && !accounts.includes(signer)) {
        say("Your wallet did not offer the account " + td.message.signer + ". Select that account in your wallet, then press Sign again.", "bad");
        $("sign").disabled = false; return;
      }
      if (!accounts.length) { say("Your wallet offered no account.", "bad"); $("sign").disabled = false; return; }
      if (!(await ensureChain(p))) { $("sign").disabled = false; return; }
      if (td) {
        say("Waiting for your signature in the wallet...");
        const signature = await p.request({ method: "eth_signTypedData_v4", params: [td.message.signer, JSON.stringify(td)] });
        if (!(await post({ signature, account: td.message.signer }))) return;
        say("Signed. The terminal has it: you can close this tab.", "good");
      } else {
        say("Waiting for you to confirm the transaction in the wallet...");
        const hash = await p.request({ method: "eth_sendTransaction", params: [{ from: accounts[0], to: tx.to, data: tx.data, value: "0x0" }] });
        if (!(await post({ tx_hash: hash, account: accounts[0] }))) {
          $("sign").disabled = true;      // it was sent: never offer to send a second one
          say("The transaction was sent (hash " + hash + ") but the terminal did not take the hash. Give it to the terminal with --tx.", "bad");
          return;
        }
        say("Sent. The terminal has the transaction hash: you can close this tab.", "good");
      }
    } catch (e) {
      say(refused(e) ? "You declined. Nothing was sent. Press the button to try again." : "The wallet said: " + ((e && e.message) || e), "bad");
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
    data = _json_for_html({"typed_data": typed_data, "chain_label": chain_label,
                           "add_chain": chain_params(int(typed_data["domain"]["chainId"]))})
    shown = html.escape(json.dumps({k: typed_data[k] for k in ("domain", "primaryType", "message")}, indent=2))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Sign the link</title>
<style nonce="{nonce}">{STYLE}</style></head><body>
<h1>Link ERC-8004 agent #{html.escape(str(msg['agentId']))} to {html.escape(agent_name)}</h1>
<p><strong>&ldquo;{html.escape(str(msg['statement']))}&rdquo;</strong></p>
<p>Your wallet shows these same words, with the date and the details below. Nothing goes on-chain and it costs no gas.</p>
<p class="dim">Signer: <code>{html.escape(str(msg['signer']))}</code></p>
<p class="dim">Check the wallet popup: network {html.escape(chain_label)}. This page only asks for this one signature and closes.</p>
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


def tx_page_html(tx: dict, *, chain_label: str, agent_name: str, summary: str, nonce: str) -> str:
    """The page for sending one transaction from the person's wallet. `tx` is `{chain_id, to, data}`; `summary` is the
    plain-words account of what it does (the CLI printed it too). Nothing is sent without the wallet's own prompt."""
    data = _json_for_html({"tx": tx, "chain_label": chain_label, "add_chain": chain_params(int(tx["chain_id"]))})
    shown = html.escape(json.dumps({"function": tx.get("function", "a contract call"), "chain id": tx["chain_id"],
                                    "to (the registry)": tx["to"], "value": "0", "data (raw, as your wallet shows it)":
                                    tx["data"]}, indent=2))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Send the registration</title>
<style nonce="{nonce}">{STYLE}</style></head><body>
<h1>Register {html.escape(agent_name)} on ERC-8004</h1>
<p>{html.escape(summary)}</p>
<p class="dim">Your wallet will ask you to confirm one transaction on {html.escape(chain_label)}. Check its prompt against
the call below before you confirm. Check the network ({html.escape(chain_label)}) and the &ldquo;to&rdquo; address
{html.escape(tx['to'])}. This page only asks for this one transaction and closes.</p>
<p><select id="wallet" hidden aria-label="wallet"></select> <button id="sign" type="button" disabled>Send the registration</button></p>
<p id="status" role="status"></p>
<p id="nowallet" class="dim" hidden>No wallet was found in this browser. Open this page where your browser wallet is, or send
the call below from your wallet some other way and give the transaction hash to the terminal with <code>--tx</code>.</p>
<p class="dim">The call (a contract call with data, value 0):</p>
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
    """One page, one signature. `start()` binds; `wait()` serves until a signature arrives or the time is up.
    `SendPage` is the same page for sending one transaction: it overrides `render` and `accept` only."""

    def __init__(self, typed_data: dict, *, chain_label: str, agent_name: str, token: str | None = None,
                 timeout: float = TIMEOUT, port: int = 0, listen: str = HOST):
        self.port_wanted, self.listen = check_port(port), validate_listen(listen)
        self.typed_data, self.chain_label, self.agent_name = typed_data, chain_label, agent_name
        self.token = token or secrets.token_urlsafe(24)
        self.timeout = timeout
        self.result: dict | None = None
        self._server: HTTPServer | None = None
        self._lock = threading.Lock()
        self._signer = str(typed_data["message"]["signer"]).lower()

    def render(self, nonce: str) -> str:
        return page_html(self.typed_data, chain_label=self.chain_label, agent_name=self.agent_name, nonce=nonce)

    def accept(self, body: Any) -> dict | None:
        """The result for a well-formed POST body, else None (a refused POST does not use the one)."""
        try:
            sig, account = body["signature"], body["account"]
        except (KeyError, TypeError):
            return None
        if not isinstance(sig, str) or not isinstance(account, str) or not _HEX.fullmatch(sig) \
                or len(sig.removeprefix("0x")) % 2 or len(sig.removeprefix("0x")) > 2 * MAX_SIG_BYTES \
                or not _ADDR.fullmatch(account) or account.lower() != self._signer:
            return None
        return {"signature": sig if sig.startswith("0x") else "0x" + sig, "account": account}

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return page_url(self.listen, self.port, self.token)

    def own_hosts(self) -> set[str]:
        """The Host values the browser sends (through a tunnel or a published port): loopback names only."""
        names = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if self.listen == "::1":
            names.add(f"[::1]:{self.port}")
        return names

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
                return self.headers.get("Host", "") in page.own_hosts()

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
                body = page.render(nonce).encode()
                self._send(200, body, "text/html; charset=utf-8", nonce)

            do_HEAD = do_GET

            def do_POST(self) -> None:
                if not self._own_host() or self._route() != "sign":
                    return self._send(404, b"not found")
                origin = self.headers.get("Origin")
                if origin is not None and origin not in {f"http://{h}" for h in page.own_hosts()}:
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
                except ValueError:
                    return self._send(400, b"malformed")
                got = page.accept(body)
                if got is None:
                    return self._send(400, b"not what this page asks for")
                with page._lock:
                    if page.result is not None:
                        return self._send(409, b"a signature was already received")
                    page.result = got
                self._send(200, b'{"ok":true}', "application/json")

        # One thread per connection (daemon, never joined), so an idle browser pre-connection cannot hold the
        # accept loop past the time limit; each connection's socket times out on its own (CONN_TIMEOUT).
        class Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6 if ":" in page.listen else socket.AF_INET

        try:
            self._server = Server((self.listen, self.port_wanted), Handler)
        except OSError as e:
            raise BrowserSignError(f"cannot listen on {self.listen}:{self.port_wanted}: {e.strerror or e}"
                                   + (" (that port is in use: pick another with --port)" if self.port_wanted else "")) from e
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


class SendPage(SigningPage):
    """One page, one transaction the person's wallet sends. The page returns only the transaction hash (and the account
    that sent it); the CLI then reads the chain itself, trusting nothing the page says. `tx` is `{chain_id, to, data}`."""

    def __init__(self, tx: dict, *, chain_label: str, agent_name: str, summary: str, token: str | None = None,
                 timeout: float = TIMEOUT, port: int = 0, listen: str = HOST):
        self.port_wanted, self.listen = check_port(port), validate_listen(listen)
        self.tx, self.summary = tx, summary
        self.typed_data = {}
        self.chain_label, self.agent_name = chain_label, agent_name
        self.token = token or secrets.token_urlsafe(24)
        self.timeout = timeout
        self.result = None
        self._server = None
        self._lock = threading.Lock()
        self._signer = ""

    def render(self, nonce: str) -> str:
        return tx_page_html(self.tx, chain_label=self.chain_label, agent_name=self.agent_name, summary=self.summary,
                            nonce=nonce)

    def accept(self, body: Any) -> dict | None:
        try:
            tx_hash, account = body["tx_hash"], body["account"]
        except (KeyError, TypeError):
            return None
        if not isinstance(tx_hash, str) or not _TX.fullmatch(tx_hash) or not isinstance(account, str) \
                or not _ADDR.fullmatch(account):
            return None
        return {"tx_hash": tx_hash.lower(), "account": account}
