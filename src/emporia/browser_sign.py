"""A one-shot local page for signing the ERC-8004 link statement (or sending the registration transaction) with a browser wallet.

Runs only while `emporia agent link-8004 NAME --browser` (or `register-8004 NAME --browser`) waits, and never inside the dashboard (which has no wallet UI).
The standard library only: `http.server`, one page, one POST. It listens on loopback at a random port by default;
`--port` fixes the port (for an SSH tunnel) and `--listen` picks the address: inside a container the page listens on the
container's own private address so the host can tunnel to it. It never listens on a public interface (`resolve_listen`).

* The address carries a one-time token (`/<token>`). Any other path, or a wrong token, is a plain 404; the Host
  header must be `emporia.localhost:PORT`, `127.0.0.1:PORT` or `localhost:PORT` (`[::1]:PORT` for an IPv6 loopback
  listener): the address the browser uses through a tunnel, never another name (so a rebinding page cannot reach it); a
  POST's Origin, when sent, must be one of those origins. The printed address is `emporia.localhost` so the wallet's
  prompt names the site "emporia" (browsers keep every `*.localhost` name on the computer itself, RFC 6761);
  `127.0.0.1` is the fallback for a browser that does not.
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
import functools
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
NAMED_HOST = "emporia.localhost"     # what the wallet shows as the site; the browser resolves it to this computer
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
    """True inside a container: `/.dockerenv` or `/run/.containerenv`, a container word in `/proc/1/cgroup` or in the
    ROOT mount's line of `/proc/self/mountinfo` (a host that merely runs containers lists their overlay mounts on
    other mount points; only this process's `/` says where it lives), or a `container` variable in the environment.
    `root` and `env` are for tests."""
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
        if rel.endswith("mountinfo"):   # field 5 is the mount point: keep the line for "/" only
            text = "\n".join(ln for ln in text.splitlines() if len(ln.split()) > 4 and ln.split()[4] == "/")
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
    """What the browser opens: a loopback name on this port (through a tunnel it is the local end). `emporia.localhost`,
    so the wallet's prompt names the site; `[::1]` for an IPv6 loopback listener."""
    return f"http://{'[::1]' if listen == '::1' else NAMED_HOST}:{port}/{token}"


def fallback_url(listen: str, port: int, token: str) -> str:
    """The same page at the numeric loopback address, for a browser that does not resolve `*.localhost`."""
    return f"http://{'[::1]' if listen == '::1' else HOST}:{port}/{token}"


def instructions(port: int, listen: str, *, container: bool, ssh: bool, hostname: str = "", token: str = "",
                 chain_label: str = "", registry: str = "", what: str = "signature") -> tuple[str, bool]:
    """What to tell the person when the page starts, for the setup detected, and whether this machine opens the browser
    (only a local, non-container session does). The browser-side URL is always http://127.0.0.1:PORT/<token>, so a
    tunnel's local port must equal PORT."""
    url = page_url(listen, port, token)
    alt = fallback_url(listen, port, token)
    target = "127.0.0.1" if listen in ("127.0.0.1", "::1") else listen
    tunnel = f"ssh -L {port}:{target}:{port} <you>@{hostname or '<this-host>'}"
    other = (f"\n  If that name does not open (some Safari versions): {alt}\n" if alt != url else "")
    if ssh or container:
        where = ("<this-host> is the server that runs this container, not the container" if container
                 else "<you>@<host> is your login on this machine")
        text = (f"  Your browser is on another computer. There, run this and keep it open:\n\n"
                f"    {tunnel}\n\n"
                f"  ({where}; the local port must stay {port}.) Then open:\n\n"
                f"    {url}\n{other}")
        if container and not ssh:
            text += (f"\n  Docker on the same computer as your browser? Publish the port on its loopback instead\n"
                     f"  (compose: ports: [\"127.0.0.1:{port}:{port}\"]; never 0.0.0.0), recreate the container, and run\n"
                     f"  this with --port {port}.\n")
    else:
        text = f"  Opening your browser at:\n\n    {url}\n{other}"
    check = "  In your wallet, check: " + ", ".join(x for x in (
        f"network {chain_label}" if chain_label else "", f"to {registry}" if registry else "",
        "value 0" if what == "transaction" else "") if x) + ".\n" if (chain_label or registry) else ""
    fallback = ("  No browser that can reach it? Ctrl-C (nothing is sent), run the command without --browser and send the\n"
                "  printed call from any wallet, then run it with --tx 0xHASH.\n" if what == "transaction" else
                "  No browser that can reach it? Ctrl-C, run the command without --browser, sign the printed message in\n"
                "  any wallet, then run it with --signature 0x....\n")
    return text + "\n" + check + fallback, not (ssh or container)


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
:root{color-scheme:dark light;--bg:#0d1210;--fg:#d6e4da;--dim:#8aa093;--acc:#6fe39a;--line:#26352c;--bad:#ff8a80;--good:#6fe39a}
@media (prefers-color-scheme:light){:root{--bg:#f6f8f6;--fg:#1b2a21;--dim:#55695c;--acc:#13733c;--line:#cdd9d0;--bad:#b00020;--good:#13733c}}
body{background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,sans-serif;margin:2.5rem auto;max-width:40rem;padding:0 1rem}
.mark{font:400 1.9rem "EmporiaPixel",ui-monospace,monospace;letter-spacing:.08em;margin:0 0 .2rem;display:flex;align-items:baseline}
.mark svg{width:.62em;height:.62em;margin-left:.45em;fill:currentColor}
.status,code{font-family:ui-monospace,monospace}.status{color:var(--acc);margin:0 0 1.4rem}
h1{font-size:1.2rem;margin:0 0 .5rem}p{margin:.5rem 0}.dim{color:var(--dim)}
.run{display:grid;grid-template-columns:max-content 1fr;gap:.35rem 1.2rem;margin:.9rem 0;padding:.7rem .9rem;
border:1px solid var(--line);font:14px/1.5 ui-monospace,monospace}.run dt{color:var(--acc)}.run dd{margin:0;overflow-wrap:anywhere}
.pic{display:flex;align-items:center;gap:.9rem;margin:.6rem 0}.pic img{border:1px solid var(--line);image-rendering:pixelated}
pre{background:transparent;border:1px solid var(--line);padding:.7rem .9rem;overflow:auto;font-size:.8rem;white-space:pre-wrap;word-break:break-all}
button{font:inherit;font-family:ui-monospace,monospace;padding:.55rem 1.1rem;margin:.4rem 0;background:var(--acc);color:var(--bg);
border:0;cursor:pointer}button:disabled{opacity:.45;cursor:default}select{font:inherit;padding:.5rem;margin:.25rem 0}
#status{min-height:1.5rem;font-family:ui-monospace,monospace}.bad{color:var(--bad)}.good{color:var(--good)}
details{margin:1rem 0}summary{cursor:pointer;color:var(--dim)}footer{margin-top:2rem;color:var(--dim);font-size:.9rem}
"""

_STAR = ('<svg viewBox="0 0 7 7" shape-rendering="crispEdges" aria-hidden="true">' + "".join(
    f'<rect x="{x}" y="{y}" width="1" height="1"/>' for x, y in (
        (3, 0), (1, 1), (3, 1), (5, 1), (2, 2), (3, 2), (4, 2), (0, 3), (1, 3), (2, 3), (3, 3), (4, 3), (5, 3), (6, 3),
        (2, 4), (3, 4), (4, 4), (1, 5), (3, 5), (5, 5), (3, 6))) + "</svg>")


def _head(nonce: str, title: str) -> str:
    """The <head>: Emporia's star as the icon (a wallet shows it beside the site in its prompt), the pixel face inline."""
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">'
            f'<meta name="application-name" content="Emporia"><title>Emporia · {title}</title>'
            f'<link rel="icon" type="image/svg+xml" href="{_icon_uri()}">'
            f'<style nonce="{nonce}">{_font_css()}{STYLE}</style></head><body>\n'
            f'<p class="mark">EMPORIA{_STAR}</p>\n')


def _masthead(line: str) -> str:
    return f'<p class="status">&gt; {line}</p>\n'


_FOOT = ('<footer>This page runs on your own machine, only while the command waits, and closes after one answer. '
         'Emporia never holds your keys or sends anything itself.</footer>\n')


@functools.lru_cache(maxsize=1)
def _font_css() -> str:
    """The wordmark's pixel face (Silkscreen, SIL OFL 1.1), inlined as on the relay's front page; empty if missing."""
    import base64
    try:
        data = (Path(__file__).parent / "static" / "Silkscreen-Regular.woff2").read_bytes()
    except OSError:
        return ""
    return ('@font-face{font-family:"EmporiaPixel";font-display:swap;'
            f'src:url(data:font/woff2;base64,{base64.b64encode(data).decode()}) format("woff2")}}\n')


@functools.lru_cache(maxsize=1)
def _icon_uri() -> str:
    """Emporia's star icon (data/icons/favicon.svg) as a data: URI, so the page loads nothing."""
    import base64
    try:
        data = (Path(__file__).parent / "data" / "icons" / "favicon.svg").read_bytes()
    except OSError:
        return "data:,"
    return "data:image/svg+xml;base64," + base64.b64encode(data).decode()

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
  const addHint = () => doc.add_chain ? " (if your wallet does not have it, add it: chain id " + wantChain + ", RPC " +
    doc.add_chain.rpcUrls[0] + ")" : "";
  window.addEventListener("eip6963:announceProvider", (e) => { if (e.detail) add(e.detail.info, e.detail.provider); });
  window.dispatchEvent(new Event("eip6963:requestProvider"));
  window.addEventListener("load", () => { setTimeout(() => { if (!found.length && window.ethereum) add(null, window.ethereum); render(); }, 300); });
  const hexChain = (n) => "0x" + n.toString(16);
  const refused = (e) => e && (e.code === 4001 || e.code === "ACTION_REJECTED");
  // 4100: the wallet has not authorized this page for that account or method (a new site to it, or another account
  // picked since). The next press asks it again for access before anything else.
  const unauthorized = (e) => e && (e.code === 4100 || /not been authori[sz]ed/i.test(String(e.message || "")));
  let askAccess = false;
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
      if (unauthorized(e)) throw e;      // not a refusal: the page must be connected first (handled by the caller)
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
      if (askAccess) {
        say("Asking your wallet to give this page access to an account...");
        await p.request({ method: "wallet_requestPermissions", params: [{ eth_accounts: {} }] });
        askAccess = false;
      }
      say("Asking your wallet for the account...");
      // Keep each address exactly as the wallet gave it (a wallet may match its authorized account by that form);
      // compare in lowercase only.
      const accounts = (await p.request({ method: "eth_requestAccounts" })).map((a) => String(a));
      const lower = accounts.map((a) => a.toLowerCase());
      if (td && !lower.includes(signer)) {
        say("Your wallet did not offer the account " + td.message.signer + ". Select that account in your wallet, then press Sign again.", "bad");
        $("sign").disabled = false; return;
      }
      if (!accounts.length) { say("Your wallet offered no account.", "bad"); $("sign").disabled = false; return; }
      if (!(await ensureChain(p))) { $("sign").disabled = false; return; }
      if (td) {
        say("Waiting for your signature in the wallet...");
        const signature = await p.request({ method: "eth_signTypedData_v4", params: [accounts[lower.indexOf(signer)], JSON.stringify(td)] });
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
        say("Sent. The terminal is confirming it on the chain; you can close this tab." +
            (doc.explorer ? " See it: " + doc.explorer + hash : ""), "good");
      }
    } catch (e) {
      if (unauthorized(e)) {
        askAccess = true;
        say("Your wallet has not given this page (" + location.host + ") access to that account or network. Switch " +
            "your wallet to " + doc.chain_label + " yourself" + addHint() + ", then press the button again: your " +
            "wallet will ask you to connect this page; pick the account you mean to use. Nothing was sent.", "bad");
        $("sign").disabled = false;
        return;
      }
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
    return _head(nonce, "Sign the link") + _masthead(f"sign in your wallet &middot; {html.escape(chain_label)}") + f"""<h1>Link ERC-8004 agent #{html.escape(str(msg['agentId']))} to {html.escape(agent_name)}</h1>
<p><strong>&ldquo;{html.escape(str(msg['statement']))}&rdquo;</strong></p>
<p>Your wallet shows these same words, with the date and the details below. Nothing goes on-chain and it costs no gas.</p>
<p class="dim">Signer: <code>{html.escape(str(msg['signer']))}</code></p>
<p class="dim">Check the wallet popup: network {html.escape(chain_label)}. This page only asks for this one signature and closes.</p>
<p><select id="wallet" hidden aria-label="wallet"></select> <button id="sign" type="button" disabled>Sign</button></p>
<p id="status" role="status"></p>
<p id="nowallet" class="dim" hidden>No wallet was found in this browser. Open this page where your browser wallet is, or sign
the message below with the signer's wallet some other way and give it to the terminal with <code>--signature</code>.</p>
<details><summary>Technical details: the typed data your wallet signs (EIP-712, domain &ldquo;Emporia agent link&rdquo;)</summary>
<pre>{shown}</pre>
</details>
""" + _FOOT + f"""<script type="application/json" id="page-data">{data}</script>
<script nonce="{nonce}">{SCRIPT}</script>
</body></html>
"""


_SEL_REGISTER = "f2c298be"           # register(string)                          (erc8004_register.SEL_REGISTER_URI)
_SEL_SET_WALLET = "2d1ef5ae"         # setAgentWallet(uint256,address,uint256,bytes) (erc8004_wallet.SEL_SET_AGENT_WALLET)
_PICTURE = re.compile(r"data:image/(?:svg\+xml|png);base64,[A-Za-z0-9+/=]+")


def describe_call(tx: dict) -> dict | None:
    """The call the wallet will send, decoded from its own bytes for a person to read: `{"kind", "rows": [(label,
    value)], "image"}`, or None when it is not one of the two ERC-8004 calls this page sends (the page then shows the
    raw call only). Values are plain text; the page escapes them."""
    try:
        from eth_abi import decode
        raw = bytes.fromhex(str(tx.get("data") or "").removeprefix("0x"))
    except (ImportError, ValueError):
        return None
    sel, body = raw[:4].hex(), raw[4:]
    try:
        if sel == _SEL_REGISTER:
            from emporia.erc8004_register import decode_data_uri
            (uri,) = decode(["string"], body)
            doc = decode_data_uri(uri)
            if doc is None:
                return {"kind": "register", "rows": [("Registration", "a link, not an on-chain file: " + uri[:200])],
                        "image": None}
            svc = next((x for x in doc.get("services") or [] if isinstance(x, dict)), {})
            image = doc.get("image") if isinstance(doc.get("image"), str) and _PICTURE.fullmatch(doc["image"]) else None
            rows = [("Name", str(doc.get("name") or "")), ("Description", str(doc.get("description") or "")),
                    ("Profile", str(svc.get("endpoint") or "")), ("Stable id", str(svc.get("stableId") or "")),
                    ("Accepts payments (x402)", "yes" if doc.get("x402Support") else "no"),
                    ("Picture", "below, stored on chain in this registration" if image else "none")]
            return {"kind": "register", "rows": [(k, v) for k, v in rows if v], "image": image,
                    "data_bytes": len(raw), "image_bytes": len(image.encode()) if image else 0}
        if sel == _SEL_SET_WALLET:
            agent_id, wallet, deadline, sig = decode(["uint256", "address", "uint256", "bytes"], body)
            until = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(deadline)))
            return {"kind": "set_wallet", "image": None, "rows": [
                ("Identity", f"ERC-8004 agent #{agent_id}"), ("Paid at (new agent wallet)", str(wallet)),
                ("Consent valid until", until),
                ("Consent", f"signed by that wallet ({len(sig)} bytes); the registry checks it"),
                ("Unchanged", "who owns the identity, and every balance")], "data_bytes": len(raw)}
    except Exception:  # noqa: BLE001 — undecodable bytes: show the raw call only, never a guess
        return None
    return None


def tx_page_html(tx: dict, *, chain_label: str, agent_name: str, summary: str, nonce: str) -> str:
    """The page for sending one transaction from the person's wallet. `tx` is `{chain_id, to, data}`; `summary` is the
    plain-words account of what it does (the CLI printed it too). The call is shown decoded from the exact bytes the
    wallet will send (`describe_call`), the raw call folded below it. Nothing is sent without the wallet's own prompt."""
    from emporia.networks import NETWORKS
    net = NETWORKS.get(f"eip155:{int(tx['chain_id'])}")
    data = _json_for_html({"tx": tx, "chain_label": chain_label, "add_chain": chain_params(int(tx["chain_id"])),
                           "explorer": net.explorer if net and net.explorer else ""})
    shown = html.escape(json.dumps({"function": tx.get("function", "a contract call"), "chain id": tx["chain_id"],
                                    "to (the registry)": tx["to"], "value": "0", "data (raw, as your wallet shows it)":
                                    tx["data"]}, indent=2))
    call = describe_call(tx)
    fn_name = "setAgentWallet" if call and call["kind"] == "set_wallet" else "register"
    name = html.escape(agent_name)
    if call and call["kind"] == "set_wallet":
        title, head, button = "Set the agent wallet", f"Set where {name}&rsquo;s ERC-8004 identity is paid", "Send it"
    else:
        title, head, button = "Send the registration", f"Register {name} on ERC-8004", "Send the registration"
    cost = "test gas only (no value)" if "testnet" in chain_label else "gas only (no value)"
    rows = list(call["rows"]) if call else []
    rows += [("Network", f"{chain_label}, chain id {tx['chain_id']}"), ("Sent to", f"{tx['to']} (the ERC-8004 identity registry)"),
             ("Cost", cost)]
    table = "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>" for k, v in rows)
    picture = (f'<div class="pic"><img src="{html.escape(call["image"], quote=True)}" alt="{name}&rsquo;s picture, as '
               f'registered" width="96" height="96"><span class="dim">The picture that goes on chain with this '
               'registration.</span></div>') if call and call.get("image") else ""
    if call and call["kind"] == "register":
        inside = (f'{call["data_bytes"]:,} bytes: ' + (f'{call["image_bytes"]:,} of them are {name}&rsquo;s picture '
                  '(image data, which looks like random letters), ' if call["image_bytes"] else "")
                  + "the rest is the text above and the call&rsquo;s own framing. It holds no signature.")
    elif call:
        inside = (f'{call["data_bytes"]:,} bytes: the identity number, the wallet, the deadline and that wallet&rsquo;s '
                  "65-byte consent signature, each shown above.")
    decoded = (f'<p class="dim">Read here from the exact bytes your wallet will send. Your wallet names the same call '
               f'({html.escape(fn_name)}) on the registry, with its fee; the full data sits behind its Data or Hex tab, '
               f'{inside}</p>') if call else '<p class="dim">This call could not be read here: check it against the details below.</p>'
    return _head(nonce, title) + _masthead(f"one transaction &middot; {html.escape(chain_label)}") + f"""<h1>{head}</h1>
<p>{html.escape(summary)}</p>
<dl class="run">{table}</dl>
{picture}
{decoded}
<p class="dim">Your wallet will ask you to confirm one transaction on {html.escape(chain_label)}. Check the network
({html.escape(chain_label)}) and the &ldquo;to&rdquo; address {html.escape(tx['to'])} in its prompt.
This page only asks for this one transaction and closes.</p>
<p><select id="wallet" hidden aria-label="wallet"></select> <button id="sign" type="button" disabled>{button}</button></p>
<p id="status" role="status"></p>
<p id="nowallet" class="dim" hidden>No wallet was found in this browser. Open this page where your browser wallet is, or send
the call below from your wallet some other way and give the transaction hash to the terminal with <code>--tx</code>.</p>
<details><summary>Technical details: the call data (a wallet shows it under Data or Hex)</summary>
<pre>{shown}</pre>
</details>
""" + _FOOT + f"""<script type="application/json" id="page-data">{data}</script>
<script nonce="{nonce}">{SCRIPT}</script>
</body></html>
"""


def csp(nonce: str) -> str:
    """Nothing loads from anywhere. `data:` images (the star icon, the registration's own picture) and the inline
    pixel face are the only non-script inline resources; an image or a font cannot run script."""
    return (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src data:; font-src data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


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
        names = {f"{NAMED_HOST}:{self.port}", f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
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
