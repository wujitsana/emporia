"""`emporia agent about | x-claim | x-link | link-8004 | unlink-8004 | register-8004`: the card's bio and links, from a person's terminal.

* `about NAME "text"` sets the bio (`EMPORIA_ABOUT`, at most 160 characters, plain text, no link) and registers
  the agent again so the relay's card carries it; `about NAME` shows it (and a suggestion); `about NAME ""`
  clears it. A person's setting: refused while an unattended turn (a relay-started one, or rounds) is live in the
  agent's profile, and held from an agent's own terminal by the key guard.
* `x-claim NAME --handle H [--style plain|arena|lab] [--text "..."]` makes the signed claim line and the posts
  to show it (the stock ones, or your own words with the line added last), and the bio form (the line alone).
  It posts nothing.
* `x-link NAME --handle H --post ID|bio` records on the card where the claim line was shown.
* `link-8004 NAME --chain C --agent-id N [--signer 0x..]` reads an ERC-8004 identity on-chain and prints the owner,
  binding and tier it expects, the privacy note and the EIP-712 message to sign in a wallet; with `--signature 0x..
  --issued-at T` it verifies that locally, then signs and posts the link (`chain_link`). `unlink-8004` removes it
  (a signed removal; the marker stays). Both are refused while an unattended turn is live.
* `register-8004 NAME --chain base-sepolia` builds the call that gives the agent its own ERC-8004 identity (the
  registration file as an on-chain `data:` URI) for the person's own wallet to send; Emporia sends nothing and spends
  nothing. `--browser` has the person's browser wallet send it from a one-shot local page. `--tx HASH` reads the
  receipt, checks the registration names this agent, and continues into `link-8004`. Testnet only for now.
  `--from-agent-wallet --max-gas N` has the agent's own Emporia EVM wallet send it instead (you read the cost and
  answer y/N; one transaction; the wallet then owns the identity and signs its link itself).
* `set-8004-wallet NAME --chain base-sepolia --agent-id N` points an identity's `agentWallet` at the agent's own wallet:
  the wallet signs the registry's consent, the identity's owner sends the call (printed, `--browser`, or
  `--from-agent-wallet --max-gas N` when the agent's own wallet is the owner); `--tx HASH` reads the result.

Works for a Hermes agent profile and for an agent made with `emporia agent init` (its key directory).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from emporia import wire


def _cli():
    from emporia import cli
    return cli


class _Target:
    """One agent, however it is run: a Hermes profile, or a key directory made by `agent init`."""

    def __init__(self, name: str):
        cli = _cli()
        from emporia import agent_init
        self.name = name
        profile = {p.name: p for p, _ in cli._agent_profiles(cli.profiles_root())}.get(name)
        self.profile: Path | None = profile
        self.key_dir: Path | None = None
        if profile is None:
            kd = agent_init.default_dir(name)
            if (kd / agent_init.ENV_FILE).exists():
                self.key_dir = kd
        self.found = profile is not None or self.key_dir is not None

    def settings(self) -> dict[str, str]:
        from emporia import agent_init
        if self.profile is not None:
            return _cli().installer().harvest_env(self.profile)
        return agent_init.settings_of(self.key_dir) if self.key_dir else {}

    def set(self, updates: dict[str, str]) -> None:
        from emporia import agent_init
        if self.profile is not None:
            _cli().merge_env_file(self.profile / ".env", updates)
        else:
            cur = agent_init.settings_of(self.key_dir)
            cur.update({k: v for k, v in updates.items() if v != ""})
            for k, v in updates.items():
                if v == "":
                    cur.pop(k, None)
            agent_init.write_env(self.key_dir, cur)

    def run(self, code: str) -> subprocess.CompletedProcess:
        """`code` as the agent: its own env, nothing else; the last stdout line is JSON."""
        cli = _cli()
        if self.profile is not None:
            return cli._as_agent(self.profile, code)
        from emporia import agent_init
        env = {"PATH": os.defpath, "HOME": str(Path.home()), "EMPORIA_NO_DOTENV": "1",
               **agent_init.settings_of(self.key_dir)}
        return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)

    def unattended(self) -> str:
        from emporia import autonomy
        folder = (self.profile / "plugin-data" / "emporia") if self.profile is not None else (
            self.key_dir / "autonomy" if self.key_dir else None)
        return autonomy.unattended_live(folder)


_HEX64 = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")


def _result(r: subprocess.CompletedProcess) -> dict[str, Any]:
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        # a crashed helper's own words: 32-byte hex runs (a key looks like one) are never shown, a hash included
        text = _HEX64.sub("[hex removed]", (r.stderr or r.stdout).strip()[-300:])
        return {"success": False, "error": text or "no answer"}


def _draft_bio(t: _Target, name: str, notes: str, use_soul: bool) -> str | None:
    """`about NAME --draft "notes"`: the agent's own model drafts a line; in a person's terminal they keep, edit,
    redraft or drop it. Returns the text to save, or None when nothing is to be saved (also: no terminal, where
    the draft is only printed, with the command that would save it)."""
    from emporia import bio_draft
    if t.profile is None:
        print("agent about --draft: only a Hermes agent profile has a model to draft with here; write the bio "
              f"yourself: emporia agent about {name} \"text\"")
        return None
    if why := t.unattended():
        print(f"agent about: a {why} turn is running for {name}: the bio is a person's to change. Run it "
              "yourself, when no turn is live.")
        return None
    cli = _cli()
    if not cli._persons_terminal():
        text, why = bio_draft.draft_with_reason(t.profile, notes, use_soul)
        if not text:
            print(f"agent about --draft: {why}.")
        else:
            print(f"  Draft: {text!r}  (not saved: there is no terminal to keep it from)")
            print(f"  To keep it: emporia agent about {name} {json.dumps(text)}")
        return None
    return bio_draft.review(t.profile, notes, use_soul, read=lambda q: cli.prompt(q, ""),
                            edit=bio_draft.prefill_input)


def about(name: str, text: str | None, draft_notes: str | None = None, use_soul: bool = False) -> int:
    """`emporia agent about NAME ["text"]` / `emporia agent about NAME --draft "notes" [--use-soul]`."""
    t = _Target(name)
    if not t.found:
        print(f"agent about: no agent {name!r} here (a Hermes agent profile, or one made with `emporia agent init` in "
              "the default place, ~/.config/emporia/agents/NAME; an agent whose key directory is elsewhere is not "
              "found by name)")
        return 1
    current = (t.settings().get("EMPORIA_ABOUT") or "").strip()
    if draft_notes is not None:
        if text is not None:
            print("agent about: give the bio's text or --draft, not both")
            return 1
        text = _draft_bio(t, name, draft_notes, use_soul)
        if not text:
            print(f"  {name}: nothing saved.")
            return 0
    if text is None:
        from emporia import identity_card
        print(f"  {name}: bio {current!r}" if current else f"  {name}: no bio yet")
        if t.profile is not None and (s := identity_card.suggestion_for_profile(t.profile)) and s != current:
            print(f"  suggestion (the first line of its SOUL.md): {s!r}")
        print(f"  Set it: emporia agent about {name} \"text\" (at most {wire.ABOUT_MAX} characters, plain text, no "
              "link; \"\" clears it)")
        return 0
    if why := t.unattended():
        print(f"agent about: a {why} turn is running for {name}: the bio is a person's to change, not something an "
              "unattended turn does. Run it yourself, when no turn is live.")
        return 1
    text = text.strip()
    if why := wire.about_problem(text):
        print(f"agent about: {why}")
        return 1
    t.set({"EMPORIA_ABOUT": text})
    print(f"  {name}: " + (f"bio set to {text!r}." if text else "bio cleared."))
    out = _result(t.run("import json\nfrom emporia import mcp_server as m\n"
                        "print(json.dumps(m.register_agent()))"))
    if out.get("success"):
        print("  The relay's card has it now (the agent registered again, signed).")
    else:
        print("  Saved. The relay's card gets it the next time the agent registers (it does when its MCP server "
              "starts; a server on another host reads the bio from emporia.env in the key directory"
              + (f"; now: {out.get('error')}" if out.get("error") else "") + ").")
    return 0


def x_claim(name: str, handle: str, style: str, text: str, bio_only: bool) -> int:
    """`emporia agent x-claim NAME --handle H`."""
    t = _Target(name)
    if not t.found:
        print(f"agent x-claim: no agent {name!r} here")
        return 1
    if not wire.clean_x_handle(handle):
        print("agent x-claim: --handle is an X handle: letters, digits and _, at most 15 (with or without the @)")
        return 1
    out = _result(t.run("import json\nfrom emporia import mcp_server as m\n"
                        f"print(json.dumps(m.x_claim_line({handle!r}, {style!r}, {text!r})))"))
    if not out.get("success"):
        print(f"agent x-claim: {out.get('error')}")
        return 1
    if bio_only:
        print(out["bio_text"])
        return 0
    print(f"  Claim for @{out['handle']}, as {name}. Post this (the dashboard's Post on X button opens X with it):\n")
    print(out["post_text"])
    print(f"\n  {out['post_length']} of {out['post_limit']} characters. Or put just the line in the account's bio:\n")
    print(out["bio_text"])
    print(f"\n  Then record it: emporia agent x-link {name} --handle {out['handle']} --post POST_ID   "
          "(POST_ID: the post's number; or `bio`)")
    print("  The relay never looks at X; another operator's agent confirms it there. "
          "Free, no API: open the post in your own X app.")
    return 0


def x_link(name: str, handle: str, post: str) -> int:
    """`emporia agent x-link NAME --handle H --post ID|bio`."""
    t = _Target(name)
    if not t.found:
        print(f"agent x-link: no agent {name!r} here")
        return 1
    clean = wire.clean_x_handle(handle)
    if not clean or wire.x_proof_problem(post):
        print("agent x-link: --handle is an X handle, and --post the post's number (digits) or bio")
        return 1
    out = _result(t.run("import json\nfrom emporia import mcp_server as m\n"
                        f"print(json.dumps(m.x_link({clean!r}, {post!r})))"))
    if not out.get("success"):
        print(f"agent x-link: {out.get('error')}" + (f" - {out['detail']}" if out.get("detail") else ""))
        return 1
    link = next((x for x in out.get("links") or [] if x.get("handle") == clean), {})
    print(f"  @{clean} is on {name}'s card as {link.get('status', 'claimed')} "
          f"(confirmed by {link.get('confirmed_by', 0)} other operators).")
    return 0



def _hash_from_timeout(e: subprocess.TimeoutExpired) -> str:
    """The transaction hash the send child wrote to stderr before it broadcast, from what the timeout captured ('')."""
    from emporia import chain_send
    raw = e.stderr if e.stderr is not None else b""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    found = re.findall(re.escape(chain_send.HASH_MARK) + r"(0x[0-9a-f]{64})\b", text)
    return found[-1] if found else ""


def _chain_run(t: _Target, fn: str, *args: Any, mod: str = "chain_link", **kwargs: Any) -> dict[str, Any]:
    """`chain_link.<fn>(*args, **kwargs)` (or `<mod>.<fn>`) as the agent; the answer, or {"error": ...}."""
    argtext = ", ".join([repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()])
    try:
        r = t.run(f"import json\nfrom emporia import {mod} as c\n"
                  "try:\n"
                  f"    out = c.{fn}({argtext})\n"
                  "    print(json.dumps({'success': True, **out}, default=str))\n"
                  "except c.LinkError as e:\n"
                  "    print(json.dumps({'success': False, 'error': str(e)}))\n")
    except subprocess.TimeoutExpired as e:
        sent = fn.startswith("send")
        extra: dict[str, Any] = {}
        if sent and (h := _hash_from_timeout(e)):
            extra["tx_hash"] = h
        return {"success": False, "timeout": True, **extra,
                "error": ("the helper timed out. " + ("The transaction may have been sent: look at the agent wallet's address "
                          "on the block explorer before running anything again (a send you repeat can mint twice)."
                          if sent else "Nothing was changed; try again."))}
    return _result(r)


def _link_target(cmd: str, name: str) -> _Target | None:
    t = _Target(name)
    if not t.found:
        print(f"agent {cmd}: no agent {name!r} here (a Hermes agent profile, or one made with `emporia agent init` in "
              "the default place, ~/.config/emporia/agents/NAME; a key directory elsewhere is not found by name)")
        return None
    if why := t.unattended():
        print(f"agent {cmd}: a {why} turn is running for {name}: an ERC-8004 link, mint or wallet change is a person's to change "
              "(it publishes a wallet address or spends from a wallet). Run it yourself, when no turn is live.")
        return None
    return t


def _agent_session() -> bool:
    """A command an agent's terminal tool started (Hermes marks those with its session variables). `--yes` skips
    the question, never this: publishing a wallet on the card is the person's act."""
    return any(k.startswith("HERMES_SESSION_") for k in os.environ)


def _refuse_in_agent_session(cmd: str) -> bool:
    if _agent_session():
        print(f"agent {cmd}: an ERC-8004 link, mint or wallet change is a person's to change (it publishes a wallet address "
              "or spends from a wallet); `--yes` does not stand in for them. Run it yourself, in your own terminal.")
        return True
    return False


def _refuse_with_desktop_control(cmd: str, what: str, t: _Target) -> bool:
    """`--browser` (the person's browser wallet signs) and `--from-agent-wallet` are refused, with no override, where
    the profile lets the agent control the desktop or a real browser (emporia.desktop_control). `--signature` and
    `--tx` (signed or sent elsewhere) are not asked."""
    from emporia import desktop_control
    if t.profile is None:                      # an agent Hermes does not run has no profile to read: unknown
        return False
    if why := desktop_control.refusal(what, t.profile):    # the NAMED agent's profile, not the one this runs in
        print(f"agent {cmd}: {why}")
        return True
    return False


def link_8004(name: str, chain: str, agent_id: str, signer: str = "", signature: str = "",
              issued_at: int = 0, yes: bool = False, browser: bool = False, wait: bool = False,
              from_agent_wallet: bool = False, port: int = 0, listen: str = "auto") -> int:
    """`emporia agent link-8004 NAME --chain C --agent-id N [--signer 0x..] [--signature 0x.. --issued-at T | --browser |
    --from-agent-wallet] [--wait]`. `--wait` first waits (reads only) until the chain's finalized state holds the agent.
    `--from-agent-wallet` has the agent's own wallet sign the statement (for an identity that wallet owns)."""
    t = _link_target("link-8004", name)
    if t is None:
        return 1
    if (port or listen != "auto") and not browser:
        print("agent link-8004: --port and --listen go with --browser")
        return 1
    if (signature or browser or from_agent_wallet) and _refuse_in_agent_session("link-8004"):
        return 1
    if browser and _refuse_with_desktop_control("link-8004", "--browser has a browser wallet sign", t):
        return 1
    if from_agent_wallet and _refuse_with_desktop_control("link-8004", "--from-agent-wallet signs with the agent's wallet", t):
        return 1
    if from_agent_wallet and (signature or issued_at or browser or signer):
        print("agent link-8004: --from-agent-wallet signs with the agent's own wallet; do not give --signature, "
              "--issued-at, --signer or --browser with it")
        return 1
    if from_agent_wallet and chain and str(agent_id or "").strip():
        return _link_own_wallet(t, name, chain, agent_id, yes, wait)
    if not chain or not str(agent_id or "").strip():
        print("agent link-8004: --chain (e.g. base, base-sepolia, robinhood) and --agent-id N are required")
        return 1
    if browser and (signature or issued_at):
        print("agent link-8004: --browser gets the signature from your browser wallet; do not give --signature or "
              "--issued-at with it")
        return 1
    if wait:
        resume = (f"emporia agent link-8004 {name} --chain {_cmd_chain(chain)} --agent-id {agent_id}"
                  + (f" --signer {signer}" if signer else "") + (" --browser" if browser else "") + " --wait")
        seen = _wait_for(lambda: _chain_run(t, "ask", chain, agent_id, signer), lambda o: not _not_yet(o),
                         f"#{agent_id} to be finalized on {_cmd_chain(chain)}", resume)
        if seen is None:
            return 1
    if browser:
        return _link_in_browser(t, name, chain, agent_id, signer, yes, port=port, listen=listen)
    if not signature:
        out = _chain_run(t, "ask", chain, agent_id, signer)
        if not out.get("success"):
            print(f"agent link-8004: {out.get('error')}")
            return 1
        here = bool(out.get("tier")) and _cli()._persons_terminal()
        path = _save_link_message(out["typed_data"]) if here else ""
        _print_ask(name, out, path)
        if not here:
            return 0
        # A person is here: take the signature now (pasted, or read from a file), instead of a second command.
        if path:
            print(f"  With a command line: cast wallet sign --data --from-file {path} --private-key ... > sig.txt\n")
        got = _cli().prompt("Paste the signature (0x...), or the path to a file holding it (Enter stops)", "").strip()
        if not got:
            return 0
        sig = _signature_from(got)
        if not sig:
            print("agent link-8004: that is neither a signature (hex, 65 bytes or more) nor a readable file holding one; nothing "
                  "recorded. Run this again when you have it.")
            return 1
        return link_8004(name, chain, agent_id, out["signer"], sig, int(out["issued_at"]), yes, False, False, False,
                         port, listen)
    if not signer or not issued_at:
        print("agent link-8004: with --signature give --signer (the wallet that signed) and --issued-at (printed "
              "beside the message)")
        return 1
    cli = _cli()
    if not yes:
        if not cli._persons_terminal():
            print("agent link-8004: recording publishes the wallet address on the card; there is no terminal to "
                  "ask in. Run it in your own terminal, or add --yes.")
            return 1
        print(f"  {chain_note(chain)}")
        print("  " + _wrap_privacy())
        if cli.prompt(f"Record {signer} as {name}'s ERC-8004 #{agent_id} on its card? [y/N]", "n").lower() not in ("y", "yes"):
            print("  nothing recorded.")
            return 0
    out = _chain_run(t, "record", chain, agent_id, signer, int(issued_at), signature)
    if not out.get("success"):
        print(f"agent link-8004: {out.get('error')}")
        return 1
    link = out.get("link") or {}
    got = out.get("checked_here") or {}
    print(f"  Checked here at block {got.get('block_number')}: {got.get('tier')}. "
          f"On {name}'s card as {link.get('status', 'claimed')}"
          + (" (testnet)." if out.get("testnet") else "."))
    return 0


def _browser_listen(cmd: str, port: int, listen: str) -> str | None:
    """Check `--port` and `--listen` for a `--browser` run: the address to listen on, or None (said)."""
    from emporia import browser_sign
    try:
        browser_sign.check_port(port)
        return browser_sign.resolve_listen(listen or "auto")
    except browser_sign.BrowserSignError as e:
        print(f"agent {cmd}: {e}")
        return None


def _announce_page(page: Any, *, chain_label: str, registry: str, what: str, lead: str) -> None:
    """Say how to reach the page for the setup detected (local, SSH, container), then open the browser only when this is
    a local session (a browser on a server or in a container is not the person's)."""
    import socket
    import webbrowser

    from emporia import browser_sign
    container, ssh = browser_sign.in_container(), browser_sign.ssh_session()
    host = "" if container else socket.gethostname()
    text, opens = browser_sign.instructions(page.port, page.listen, container=container, ssh=ssh, hostname=host,
                                            token=page.token, chain_label=chain_label, registry=registry, what=what)
    print(f"\n  {lead}\n\n{text}")
    if opens:
        try:
            webbrowser.open(page.url)       # best effort: the URL above is the fallback
        except Exception:  # noqa: BLE001
            pass


def _link_in_browser(t: _Target, name: str, chain: str, agent_id: str, signer: str, yes: bool, *,
                     timeout: float | None = None, port: int = 0, listen: str = "auto") -> int:
    """`--browser`: read the chain, serve one local page for the wallet to sign (browser_sign), then check and record
    what comes back exactly as `--signature` does. Nothing is served unless the chain backs the signer."""
    from emporia import browser_sign
    cli = _cli()
    addr = _browser_listen("link-8004", port, listen)
    if addr is None:
        return 1
    if not yes and not cli._persons_terminal():
        print("agent link-8004: recording publishes the wallet address on the card; there is no terminal to ask in. "
              "Run it in your own terminal, or add --yes.")
        return 1
    out = _chain_run(t, "ask", chain, agent_id, signer)
    if not out.get("success"):
        print(f"agent link-8004: {out.get('error')}")
        return 1
    print(f"  ERC-8004 #{out['agent_id']} on {out['chain_label']}, read at block {out['block_number']}; "
          f"owner {out['owner']}; signer {out['signer']}")
    if not out["tier"]:
        print(f"agent link-8004: the chain does not back this signer ({out.get('reason') or 'not proven'}): "
              "nothing to sign. Give --signer for the wallet that controls it.")
        return 1
    print(f"  Once signed the card will say: {out['tier']} ({out['tier_wording']}).")
    try:
        page = browser_sign.SigningPage(out["typed_data"], chain_label=out["chain_label"], agent_name=name, port=port,
                                        listen=addr, **({} if timeout is None else {"timeout": timeout})).start()
    except browser_sign.BrowserSignError as e:
        print(f"agent link-8004: {e}")
        return 1
    _announce_page(page, chain_label=out["chain_label"], registry=str(out["agent_registry"]).split(":")[2],
                   what="signature", lead=f"Open this page in the browser that has your wallet. It works while this "
                   f"command runs (up to {int(page.timeout // 60)} min); Ctrl-C cancels.")
    try:
        got = page.wait()
    except browser_sign.BrowserSignError as e:
        print(f"agent link-8004: {e}; nothing recorded")
        return 1
    except KeyboardInterrupt:
        page.close()
        print("\n  cancelled; nothing recorded.")
        return 1
    if not yes:
        print(f"  {chain_note(chain)}")
        print("  " + _wrap_privacy())
        if cli.prompt(f"Record {out['signer']} as {name}'s ERC-8004 #{agent_id} on its card? [y/N]", "n").lower() not in ("y", "yes"):
            print("  nothing recorded.")
            return 0
    rec = _chain_run(t, "record", chain, agent_id, out["signer"], int(out["issued_at"]), got["signature"])
    if not rec.get("success"):
        print(f"agent link-8004: {rec.get('error')}")
        return 1
    link = rec.get("link") or {}
    checked = rec.get("checked_here") or {}
    print(f"  Checked here at block {checked.get('block_number')}: {checked.get('tier')}. "
          f"On {name}'s card as {link.get('status', 'claimed')}"
          + (" (testnet)." if rec.get("testnet") else "."))
    return 0


def chain_note(chain: str) -> str:
    from emporia import networks
    c = networks.resolve(chain)
    return f"chain: {networks.label(c)}"


def _wrap_privacy() -> str:
    from emporia import chain_link
    return chain_link.PRIVACY_NOTE


_SIG_HEX = re.compile(r"(?:0x)?(?:[0-9a-fA-F]{2}){65,}")


def _as_sig(word: str) -> str:
    return ("0x" + word.removeprefix("0x")) if _SIG_HEX.fullmatch(word) else ""


def _signature_from(text: str) -> str:
    """A pasted signature, or the first word of the file a path names (`cast wallet sign ... > sig.txt`); "" if neither.
    Only the form is checked here: the record step verifies it against the message and the chain."""
    text = text.strip().strip("'\"")
    if _SIG_HEX.fullmatch(text):
        return _as_sig(text)
    try:
        p = Path(text).expanduser()
        if p.is_file() and p.stat().st_size < 64 * 1024:
            word = (p.read_text(errors="replace").split() or [""])[0].strip()
            return _as_sig(word)
    except (OSError, ValueError):
        pass
    return ""


def _save_link_message(typed_data: dict) -> str:
    """The typed message in a private temporary file for a command-line signer; "" if it cannot be written."""
    import tempfile
    try:
        fd, path = tempfile.mkstemp(prefix="emporia-8004-link-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(typed_data, f, indent=2)
        return path
    except OSError:
        return ""


def _print_ask(name: str, out: dict[str, Any], saved: str = "") -> None:
    """The link in words: what it is, the facts, the easy way (--browser), then how to sign elsewhere. With `saved`
    (a person at the terminal) the typed message is in that file rather than printed."""
    cmd = f"emporia agent link-8004 {name} --chain {_short(out['chain'])} --agent-id {out['agent_id']}"
    print(f"  Link ERC-8004 #{out['agent_id']} to {name}'s card: one signature from the wallet that controls it. "
          "Nothing goes on-chain and no money moves.\n")
    print(f"    {'identity:':<12}#{out['agent_id']} on {out['chain_label']} (read at block {out['block_number']})")
    print(f"    {'owner:':<12}{out['owner']}")
    if out.get("binding"):
        b = out["binding"]
        print(f"    {'bound to:':<12}{b.get('standard', '')} {b.get('address', '')}"
              + (f" token {b['token_id']}" if b.get("token_id") else ""))
    print(f"    {'signer:':<12}{out['signer']}")
    if out["tier"]:
        print(f"    {'card says:':<12}{out['tier_wording']}" + (" (testnet)" if "testnet" in out["chain_label"] else ""))
    else:
        print(f"    {'card says:':<12}nothing: {out.get('reason') or 'the chain does not back this signer'}")
    print(f"\n  {out['privacy']}\n")
    print(f"  Easiest: {cmd} --browser   (your browser wallet signs on a local page)")
    print(f"\n  Signing elsewhere: the wallet {out['signer']} signs EIP-712 typed data whose first words are\n"
          f"  \"{out['typed_data']['message']['statement']}\"")
    if saved:
        print(f"  The message is in {saved}.")
        return
    print("  With a command line, save the message below as msg.json and run "
          "`cast wallet sign --data --from-file msg.json --private-key ...`:\n")
    print(json.dumps(out["typed_data"], indent=2))
    print(f"\n  Then record it:\n\n    {cmd} --signer {out['signer']} --issued-at {out['issued_at']} --signature 0x...")


WAIT_EVERY = 15.0                 # seconds between reads while --wait waits for the chain
WAIT_LIMIT = 30 * 60.0            # --wait gives up after this long and prints the command to run again
#: what a read says while the chain has not caught up (an unmined transaction, a block not yet safe or finalized,
#: an agent the finalized state does not hold yet). Any other answer, good or bad, ends the wait.
_NOT_YET = ("not mined yet", "safe block", "finalized", "does not exist in that registry", "named no owner",
            "the agent has no owner", "could not be read")


def _not_yet(out: dict[str, Any]) -> bool:
    err = str(out.get("error") or "")
    return not out.get("success") and any(w in err for w in _NOT_YET)


_BEHIND = re.compile(r"(\d+) blocks behind")


def _blocks_left(out: dict[str, Any]) -> int | None:
    """How many blocks the chain still has to go, from a not-yet answer or a confirmed one waiting on finality."""
    if (m := _BEHIND.search(str(out.get("error") or ""))):
        return int(m.group(1))
    fin, blk = out.get("finalized_block"), out.get("block_number")
    if isinstance(fin, int) and isinstance(blk, int) and fin < blk:
        return blk - fin
    return None


def _wait_for(call: Any, ready: Any, what: str, resume: str) -> dict[str, Any] | None:
    """`--wait`: run `call()` every WAIT_EVERY seconds, up to WAIT_LIMIT, until `ready(answer)`; one short progress line
    per try. Returns the answer, or None when it ran out of time or the person pressed Ctrl-C (it then prints the exact
    command that picks up from here). Reads only."""
    start = time.monotonic()
    try:
        while True:
            out = call()
            if ready(out):
                return out
            waited = int(time.monotonic() - start)
            if waited >= WAIT_LIMIT:
                print(f"  gave up waiting for {what} after {waited // 60} minutes. Pick up from here:\n\n    {resume}")
                return None
            left = _blocks_left(out)
            togo = (f"about {max(1, left * 2 // 60)} min to go ({left} blocks); " if left else "")
            print(f"  waiting for {what} ({togo}{waited // 60}m{waited % 60:02d}s so far; checking every "
                  f"{int(WAIT_EVERY)}s; Ctrl-C stops and prints how to pick up)")
            time.sleep(WAIT_EVERY)
    except KeyboardInterrupt:
        print(f"\n  stopped waiting; nothing was changed. Pick up from here:\n\n    {resume}")
        return None


def _short(caip2: str) -> str:
    from emporia import chain_link
    return next((k for k, v in chain_link.chains().items() if v == caip2), caip2)


def _cmd_chain(chain: str) -> str:
    return _short(chain) if chain.startswith("eip155:") else chain


def register_8004(name: str, chain: str, tx: str = "", sender: str = "", yes: bool = False,
                  browser: bool = False, wait: bool = False, from_agent_wallet: bool = False,
                  max_gas: int = 0, no_image: bool = False, port: int = 0, listen: str = "auto") -> int:
    """`emporia agent register-8004 NAME --chain C [--from 0x..] [--browser | --tx HASH | --from-agent-wallet --max-gas N]
    [--no-image] [--wait]`: the own-wallet path. The registration carries the agent's token picture unless `--no-image`. `--wait` (with --tx, --browser or --from-agent-wallet) waits for the receipt to be
    safe and the block finalized, then links."""
    t = _link_target("register-8004", name)
    if t is None:
        return 1
    if (port or listen != "auto") and not browser:
        print("agent register-8004: --port and --listen go with --browser")
        return 1
    if _refuse_in_agent_session("register-8004"):
        return 1
    if not chain:
        print("agent register-8004: --chain is required (base-sepolia is the only one open for now)")
        return 1
    if browser and _refuse_with_desktop_control("register-8004", "--browser has a browser wallet send", t):
        return 1
    if from_agent_wallet and _refuse_with_desktop_control("register-8004", "--from-agent-wallet sends from the agent's wallet", t):
        return 1
    if browser and tx:
        print("agent register-8004: --browser sends the transaction; --tx reads one already sent: give one of them")
        return 1
    if from_agent_wallet and (browser or tx or sender):
        print("agent register-8004: --from-agent-wallet sends from the agent's own wallet; do not give --browser, "
              "--tx or --from with it")
        return 1
    if max_gas and not from_agent_wallet:
        print("agent register-8004: --max-gas goes with --from-agent-wallet")
        return 1
    if no_image and tx:
        print("agent register-8004: --no-image shapes the registration that gets sent; --tx reads one already sent")
        return 1
    if wait and not (tx or browser or from_agent_wallet):
        print("agent register-8004: --wait goes with --tx (a transaction already sent), --browser or "
              "--from-agent-wallet (which send it)")
        return 1
    if browser or from_agent_wallet:
        wait = True             # a person is at the terminal and just sent it: keep reading until it is final
    if from_agent_wallet:
        return _register_from_agent_wallet(t, name, chain, yes, max_gas, wait, no_image)
    if tx:
        return _register_after(t, name, chain, tx, yes, wait=wait)
    out = _chain_run(t, "build", chain, sender, mod="chain_register", no_image=no_image)
    if not out.get("success"):
        print(f"agent register-8004: {out.get('error')}")
        return 1
    _print_register(name, out, browser)
    if not browser:
        return 0
    return _register_in_browser(t, name, chain, out, yes, wait, port, listen)


def _person_at_terminal(cmd: str) -> bool:
    """Sending from the agent's own wallet needs a person at a terminal, always: `--yes` never stands in for them."""
    if _cli()._persons_terminal():
        return True
    print(f"agent {cmd}: sending from the agent's own wallet is the person's act: run it in your own terminal, where "
          "you can answer the question. (--yes does not answer it.)")
    return False


def _ask_to_send(question: str) -> bool:
    """The person's y/N before an agent wallet sends. Always asked; there is no way around it."""
    if _cli().prompt(question, "n").lower() not in ("y", "yes"):
        print("  nothing sent.")
        return False
    return True


def _register_from_agent_wallet(t: _Target, name: str, chain: str, yes: bool, max_gas: int, wait: bool,
                                no_image: bool = False) -> int:
    """`--from-agent-wallet`: quote (read-only), show the cost, ask, then send one transaction from the agent's own wallet
    and go on as `--tx` does. The wallet that sends owns the new identity, and signs its link itself."""
    from emporia import chain_send
    if not _person_at_terminal("register-8004"):
        return 1
    out = _chain_run(t, "quote_register", chain, max_gas or 0, mod="chain_register", no_image=no_image)
    if not out.get("success"):
        print(f"agent register-8004: {out.get('error')}")
        return 1
    print(f"  Register {name} on ERC-8004, {out['chain_label']}, from {name}'s own wallet\n")
    print("  " + AGENT_WALLET_EXPLAIN + "\n")
    if out.get("local_relay"):
        print(f"  Note: the registration points at {out['relay']}, which is this machine. Other people cannot reach "
              "it; set the agent's relay to its public address first if that matters.\n")
    print("  What goes on-chain:")
    print(_file_text(out))
    print(f"\n  to:          {out['registry']}  (the ERC-8004 identity registry, chain id {out['chain_id']})")
    print("  function:    register(agentURI)")
    print(f"  checked:     the registry would accept it and mint agent #{out['would_register_id']} "
          "(simulated now; the number can change if someone registers first)")
    for line in chain_send.describe(out["quote"]):
        print("  " + line)
    if not _ask_to_send(f"Send it from {out['wallet']} on {out['chain_label']}? [y/N]"):
        return 0
    sent = _chain_run(t, "send_register", chain, max_gas, out["calldata_digest"], out["quote"],
                      mod="chain_register", no_image=no_image)
    if not sent.get("success"):
        print(f"agent register-8004: {sent.get('error')}")
        if sent.get("timeout"):
            print(f"  The wallet is {out['wallet']}.")
            if sent.get("tx_hash"):
                print(f"  The transaction, if it was sent, is {sent['tx_hash']}: read it with --tx {sent['tx_hash']}.")
        return 1
    print(f"  Sent transaction {sent['tx_hash']} from {sent['wallet']}.")
    return _register_after(t, name, chain, sent["tx_hash"], yes, waiting=True, wait=wait)


AGENT_WALLET_EXPLAIN = ("The agent's own Emporia EVM wallet sends the transaction and pays its gas in Base Sepolia test "
                        "ETH, so that wallet owns the new identity (and is its agentWallet). This is the only thing it "
                        "sends: one transaction, no more. Afterwards the registration is public.")


def _file_text(out: dict[str, Any]) -> str:
    """What goes on-chain, in words: one line per field of the registration file, the picture as a line saying it is
    included and how large it is (or that it is left out). The call data carries the same file."""
    f = out["file"]
    svc = next((x for x in f.get("services") or [] if isinstance(x, dict)), {})
    n = out.get("picture_bytes") or 0
    rows = [("name", f.get("name", "")), ("description", f.get("description", "")),
            ("profile", svc.get("endpoint", "")), ("stable id", svc.get("stableId", "")),
            ("payments", "accepts x402 payments" if f.get("x402Support") else "does not take payments"),
            ("picture", f"Picture: included, the agent's token picture ({n} bytes of SVG, inside the registration)"
             if "image" in f else "Picture: left out (--no-image); the registration has no image")]
    return "\n".join(f"    {k + ':':<13}{v}" for k, v in rows if v)


def _print_register(name: str, out: dict[str, Any], browser: bool = False) -> None:
    """The registration in words. The call data (for pasting into a wallet by hand) only without --browser, labeled as
    the technical part; with --browser the page and the wallet carry it."""
    from emporia import networks
    print(f"  Register {name} on ERC-8004, {out['chain_label']}\n")
    print("  " + out["explain"] + "\n")
    if out.get("local_relay"):
        print(f"  Note: the registration points at {out['relay']}, which is this machine. Other people cannot reach "
              "it; set the agent's relay to its public address first if that matters.\n")
    print("  What goes on-chain:")
    print(_file_text(out))
    print(f"\n    {'registry:':<13}{out['registry']}  (the ERC-8004 identity registry, chain id {out['chain_id']})")
    print(f"    {'checked:':<13}the registry would accept it and mint agent #{out['would_register_id']} "
          "(simulated now; the number can change if someone registers first)")
    print(f"    {'cost:':<13}about {out['gas_estimate']:,} gas (test ETH, value 0; the wallet shows the price)")
    if browser:
        return
    n = networks.NETWORKS.get(out["chain"])
    explorer = f"{n.address_explorer}{out['registry']}#writeProxyContract" if n and n.address_explorer else ""
    print(f"\n  Easiest: let your browser wallet send it from a page that shows all of this: "
          f"emporia agent register-8004 {name} --chain {_cmd_chain(out['chain'])} --browser")
    print("\n  Sending it yourself (technical: for a wallet that takes call data):")
    print(f"    chain id:   {out['chain_id']}  ({out['chain_label']})")
    print(f"    to:         {out['registry']}")
    print(f"    function:   register(string agentURI), selector {out['selector']}")
    print(f"    calldata:   {out['calldata']}")
    print(f"    agentURI:   {out['agent_uri']}")
    print("  In any wallet that can send a contract call with data, paste the chain, `to` and `calldata`. Or "
          + (f"open {explorer} and use the write-as-proxy tab: register with the agentURI above."
             if explorer else "use the explorer's write-as-proxy tab for the registry: register with the agentURI above."))
    print(f"\n  When it is mined: emporia agent register-8004 {name} --chain {_cmd_chain(out['chain'])} --tx 0xHASH")


def _register_in_browser(t: _Target, name: str, chain: str, built: dict[str, Any], yes: bool, wait: bool = False,
                         port: int = 0, listen: str = "auto") -> int:
    """`--browser`: serve one local page that has the person's wallet send the call, then continue as `--tx`."""
    from emporia import browser_sign
    cli = _cli()
    addr = _browser_listen("register-8004", port, listen)
    if addr is None:
        return 1
    if not yes and not cli._persons_terminal():
        print("agent register-8004: sending is the person's act and there is no terminal to ask in. Run it in your own "
              "terminal, or add --yes.")
        return 1
    if not yes and cli.prompt(f"Open the page that asks your wallet to send this on {built['chain_label']}? [y/N]",
                              "n").lower() not in ("y", "yes"):
        print("  nothing sent.")
        return 0
    summary = (f"Sends register(agentURI) to the ERC-8004 identity registry {built['registry']} on "
               f"{built['chain_label']}, from the account you pick in your wallet. That account will own the new "
               f"identity. Emporia sends nothing itself; your wallet shows the gas"
               + (" (test ETH, no value)." if "testnet" in built["chain_label"] else ".")
               + " Your wallet shows this call as raw data; this page shows it decoded.")
    try:
        page = browser_sign.SendPage({"chain_id": built["chain_id"], "to": built["registry"], "data": built["calldata"],
                                      "function": "register(string agentURI)"},
                                     chain_label=built["chain_label"], agent_name=name, summary=summary, port=port,
                                     listen=addr).start()
    except browser_sign.BrowserSignError as e:
        print(f"agent register-8004: {e}")
        return 1
    _announce_page(page, chain_label=built["chain_label"], registry=built["registry"], what="transaction",
                   lead=f"Open this page in the browser that has your wallet. It works while this command runs "
                   f"(up to {int(page.timeout // 60)} min); Ctrl-C cancels.")
    try:
        got = page.wait()
    except browser_sign.BrowserSignError as e:
        print(f"agent register-8004: {e}; nothing sent")
        return 1
    except KeyboardInterrupt:
        page.close()
        print("\n  cancelled; nothing sent (if your wallet already sent it, use --tx with its hash).")
        return 1
    print(f"  Your wallet sent transaction {got['tx_hash']} from {got['account']}.")
    return _register_after(t, name, chain, got["tx_hash"], yes, waiting=True, wait=wait, browser=True, port=port,
                           listen=listen)


def _register_after(t: _Target, name: str, chain: str, tx: str, yes: bool, *, waiting: bool = False,
                    wait: bool = False, browser: bool = False, port: int = 0, listen: str = "auto") -> int:
    """Read the receipt, say what was registered, and go on to link-8004 for that agent. With `wait`, reads again every
    15 seconds until the receipt is at the safe block and that block is finalized (what the link reads at)."""
    def ask() -> dict[str, Any]:
        return _chain_run(t, "confirm", chain, tx, mod="chain_register")

    if wait:
        early = _chain_run(t, "peek", chain, tx, mod="chain_register")
        if early.get("success") and early.get("behind"):
            print(f"\n  {name} is ERC-8004 #{early['agent_id']} on {early['chain_label']}, owned by {early['owner']} "
                  f"(from the receipt: mined, not final yet). The chain makes it final in about "
                  f"{max(1, early['behind'] * 2 // 60)} min; this waits, then links it.\n")

        def done(o: dict[str, Any]) -> bool:
            if not o.get("success"):
                return not _not_yet(o)
            fin = o.get("finalized_block")
            return fin is None or fin >= o["block_number"]
        out = _wait_for(ask, done, "the transaction to be safe and finalized",
                        f"emporia agent register-8004 {name} --chain {_cmd_chain(chain)} --tx {tx} --wait")
        if out is None:
            return 1
    else:
        out = ask()
    if not out.get("success"):
        print(f"agent register-8004: {out.get('error')}")
        if waiting:
            print(f"  Once it is mined: emporia agent register-8004 {name} --chain {_cmd_chain(chain)} --tx {tx}")
        return 1
    print(f"  Registered: ERC-8004 #{out['agent_id']} on {out['chain_label']}, owned by {out['owner']} "
          f"(block {out['block_number']}).")
    print(f"  Two-way: its on-chain registration names {name} (stable id {out['stable_id'][:12]}...).")
    own = bool(out.get("owner_is_agent_wallet"))
    if own:
        print(f"  Owned by {name}'s own wallet, so it signs the link itself.")
    fin = out.get("finalized_block")
    cmd = f"emporia agent link-8004 {name} --chain {_cmd_chain(chain)} --agent-id {out['agent_id']}"
    if fin is None or fin < out["block_number"]:
        gap = "" if fin is None else f" (about {max(1, (out['block_number'] - fin) * 2 // 60)} minutes at 2-second blocks)"
        print(f"\n  Linking reads the chain at its finalized block, which has not reached yours yet{gap}. "
              f"Run this when it has:\n\n    "
              + (f"{cmd} --from-agent-wallet --wait" if own else f"{cmd}   (add --browser to sign in your browser wallet)"))
        return 0
    print(f"\n  Next: linking it to {name}'s card (the same as `{cmd}{' --from-agent-wallet' if own else ''}"
          f"{' --browser' if browser and not own else ''}`).\n")
    if own:
        rc = _link_own_wallet(t, name, chain, out["agent_id"], yes, wait)
    else:
        rc = link_8004(name, chain, out["agent_id"], "", "", 0, yes, browser, wait, False, port, listen)
    paid = ""
    if rc == 0 and not own and _cli()._persons_terminal() and _cli().prompt(
            f"Also set {name}'s own wallet as where #{out['agent_id']} is paid (one more transaction from your wallet)? "
            "[Y/n]", "y").lower() in ("y", "yes", ""):
        if set_8004_wallet(name, chain, out["agent_id"], yes=yes, browser=browser, wait=True, port=port,
                           listen=listen) == 0:
            paid = "set"
    _summary(name, out, rc == 0, paid, own)
    return rc


def _summary(name: str, out: dict[str, Any], linked: bool, paid: str, own: bool) -> None:
    """The closing lines: what now exists, with explorer links (public pages; nothing is fetched here)."""
    from emporia import networks
    n = networks.NETWORKS.get(out["chain"])
    reg_addr = networks.ERC8004_REGISTRIES.get(out["chain"], "")
    lines = [f"  {name} ↔ ERC-8004 #{out['agent_id']} on {out['chain_label']}"
             + ("" if linked else " (registered; not linked to the card yet)"),
             f"    {'owner:':<14}{out['owner']}" + (f" ({name}'s own wallet)" if own else "")]
    if paid:
        lines.append(f"    {'paid at:':<14}{name}'s own wallet")
    if n and n.explorer:
        lines.append(f"    {'registration:':<14}{n.explorer}{out['tx']}")
    if n and n.token_explorer and reg_addr:
        lines.append(f"    {'identity:':<14}{n.token_explorer.format(addr=reg_addr, id=out['agent_id'])}")
    print("\n  Done.\n" + "\n".join(lines))


def _link_own_wallet(t: _Target, name: str, chain: str, agent_id: str, yes: bool, wait: bool) -> int:
    """`link-8004 --from-agent-wallet`: the agent's own wallet owns the identity, so it signs the link statement itself
    (the key stays in the signer; nothing is sent on-chain). Same checks, privacy note and question as a browser wallet."""
    cli = _cli()
    if not cli._persons_terminal():
        print("agent link-8004: signing with the agent's own wallet is the person's act: run it in your own terminal. "
              "(--yes does not stand in for that; it only skips the question about publishing.)")
        return 1
    if wait:
        resume = (f"emporia agent link-8004 {name} --chain {_cmd_chain(chain)} --agent-id {agent_id} "
                  "--from-agent-wallet --wait")
        seen = _wait_for(lambda: _chain_run(t, "own_wallet_ask", chain, agent_id), lambda o: not _not_yet(o),
                         f"#{agent_id} to be finalized on {_cmd_chain(chain)}", resume)
        if seen is None:
            return 1
    out = _chain_run(t, "own_wallet_ask", chain, agent_id)
    if not out.get("success"):
        print(f"agent link-8004: {out.get('error')}")
        return 1
    print(f"  ERC-8004 #{out['agent_id']} on {out['chain_label']}, read at block {out['block_number']}; "
          f"owner {out['owner']}; signer {out['signer']} ({name}'s own wallet)")
    if not out["tier"]:
        print(f"agent link-8004: the chain does not back {name}'s own wallet as controlling it "
              f"({out.get('reason') or 'not proven'}): nothing to sign. Use the wallet that owns the identity "
              "(`--browser`).")
        return 1
    print(f"  Once signed the card will say: {out['tier']} ({out['tier_wording']}).")
    if not yes:
        print(f"  {chain_note(chain)}")
        print("  " + _wrap_privacy())
        if cli.prompt(f"Record {out['signer']} as {name}'s ERC-8004 #{agent_id} on its card? [y/N]", "n").lower() not in ("y", "yes"):
            print("  nothing recorded.")
            return 0
    rec = _chain_run(t, "link_own_wallet", chain, agent_id)
    if not rec.get("success"):
        print(f"agent link-8004: {rec.get('error')}")
        return 1
    link = rec.get("link") or {}
    checked = rec.get("checked_here") or {}
    print(f"  Checked here at block {checked.get('block_number')}: {checked.get('tier')}. "
          f"On {name}'s card as {link.get('status', 'claimed')}"
          + (" (testnet)." if rec.get("testnet") else "."))
    return 0


def set_8004_wallet(name: str, chain: str, agent_id: str, tx: str = "", yes: bool = False, browser: bool = False,
                    wait: bool = False, from_agent_wallet: bool = False, max_gas: int = 0,
                    port: int = 0, listen: str = "auto") -> int:
    """`emporia agent set-8004-wallet NAME --chain C --agent-id N [--browser | --from-agent-wallet --max-gas N | --tx HASH]
    [--wait]`: point the identity's `agentWallet` at the agent's own wallet. The agent's wallet signs the registry's
    consent here; the identity's owner sends the call (printed, `--browser`, or the agent's own wallet when it is the
    owner). `--tx` reads the result at the safe block."""
    t = _link_target("set-8004-wallet", name)
    if t is None:
        return 1
    if (port or listen != "auto") and not browser:
        print("agent set-8004-wallet: --port and --listen go with --browser")
        return 1
    if _refuse_in_agent_session("set-8004-wallet"):
        return 1
    if not chain or not str(agent_id or "").strip():
        print("agent set-8004-wallet: --chain (base-sepolia is the only one open for now) and --agent-id N are required")
        return 1
    if sum(bool(x) for x in (browser, from_agent_wallet, tx)) > 1:
        print("agent set-8004-wallet: give one of --browser (your wallet owns the identity), --from-agent-wallet (the "
              "agent's own wallet owns it) or --tx (read a transaction already sent)")
        return 1
    if max_gas and not from_agent_wallet:
        print("agent set-8004-wallet: --max-gas goes with --from-agent-wallet")
        return 1
    if wait and not (tx or browser or from_agent_wallet):
        print("agent set-8004-wallet: --wait goes with --tx, --browser or --from-agent-wallet (which send it)")
        return 1
    if tx:
        return _wallet_after(t, name, chain, agent_id, tx, wait=wait)
    if browser or from_agent_wallet:
        wait = True             # a person is at the terminal and just sent it: keep reading until it is safe
    if from_agent_wallet:
        return _set_wallet_from_agent_wallet(t, name, chain, agent_id, yes, max_gas, wait)
    out = _chain_run(t, "build", chain, agent_id, mod="chain_wallet")
    if not out.get("success"):
        print(f"agent set-8004-wallet: {out.get('error')}")
        return 1
    if _print_set_wallet(name, out, browser):
        return 0
    if not browser:
        print(f"\n  Or let the owner's browser wallet send it: emporia agent set-8004-wallet {name} --chain "
              f"{_cmd_chain(out['chain'])} --agent-id {out['agent_id']} --browser")
        print(f"  When it is mined: emporia agent set-8004-wallet {name} --chain {_cmd_chain(out['chain'])} "
              f"--agent-id {out['agent_id']} --tx 0xHASH")
        return 0
    return _set_wallet_in_browser(t, name, chain, out, yes, wait, port, listen)


def _print_set_wallet(name: str, out: dict[str, Any], browser: bool = False) -> bool:
    """The call, in words. True when there is nothing to do (the identity already names the agent's wallet)."""
    import datetime
    print(f"  Set ERC-8004 #{out['agent_id']}'s agent wallet on {out['chain_label']} to {name}'s own wallet\n")
    print("  " + out["explain"] + "\n")
    print(f"    identity:    #{out['agent_id']}, owned by {out['owner']}")
    print(f"    new wallet:  {out['new_wallet']}  ({name}'s own Emporia EVM wallet; it signed the consent)")
    print(f"    now names:   {out['current_wallet']}")
    if out["already"]:
        print("\n  It already names that wallet: nothing to do.")
        return True
    when = datetime.datetime.fromtimestamp(out["deadline"], datetime.UTC).strftime("%H:%M:%S UTC")
    print("    checked:     the registry would accept it from the owner (simulated now)")
    print(f"    valid until: {when}, about {out['valid_seconds'] // 60} minutes: the owner must send it before then "
          "(run this again for a fresh consent)")
    print(f"    registry:    {out['registry']}  (the ERC-8004 identity registry, chain id {out['chain_id']})")
    if "gas_estimate" in out:
        print(f"    gas:         about {out['gas_estimate']:,} (test ETH, paid by the owner)")
    if not browser:
        print("\n  Sending it yourself (technical: for a wallet that takes call data):")
        print(f"    function:    setAgentWallet(uint256,address,uint256,bytes), selector {out['selector']}")
        print(f"    calldata:    {out['calldata']}")
    return False


def _set_wallet_from_agent_wallet(t: _Target, name: str, chain: str, agent_id: str, yes: bool, max_gas: int,
                                  wait: bool) -> int:
    """The agent's own wallet owns the identity: it signs the consent and sends the call. Quote, ask, one transaction."""
    from emporia import chain_send
    if not _person_at_terminal("set-8004-wallet"):
        return 1
    out = _chain_run(t, "quote", chain, agent_id, max_gas or 0, mod="chain_wallet")
    if not out.get("success"):
        print(f"agent set-8004-wallet: {out.get('error')}")
        return 1
    if _print_set_wallet(name, out):
        return 0
    for line in chain_send.describe(out["quote"]):
        print("    " + line)
    if not _ask_to_send(f"Send it from {out['wallet']} on {out['chain_label']}? [y/N]"):
        return 0
    sent = _chain_run(t, "send", chain, agent_id, max_gas, out["owner"], out["quote"], mod="chain_wallet")
    if not sent.get("success"):
        print(f"agent set-8004-wallet: {sent.get('error')}")
        if sent.get("timeout"):
            print(f"  The wallet is {out['wallet']}.")
            if sent.get("tx_hash"):
                print(f"  The transaction, if it was sent, is {sent['tx_hash']}: read it with --tx {sent['tx_hash']}.")
        return 1
    print(f"  Sent transaction {sent['tx_hash']} from {sent['wallet']}.")
    return _wallet_after(t, name, chain, agent_id, sent["tx_hash"], waiting=True, wait=wait)


def _set_wallet_in_browser(t: _Target, name: str, chain: str, built: dict[str, Any], yes: bool, wait: bool,
                           port: int = 0, listen: str = "auto") -> int:
    """`--browser`: one local page that has the owner's wallet send the call, then continue as `--tx`."""
    from emporia import browser_sign
    cli = _cli()
    addr = _browser_listen("set-8004-wallet", port, listen)
    if addr is None:
        return 1
    if not yes and not cli._persons_terminal():
        print("agent set-8004-wallet: sending is the person's act and there is no terminal to ask in. Run it in your "
              "own terminal, or add --yes.")
        return 1
    if not yes and cli.prompt(f"Open the page that asks your wallet to send this on {built['chain_label']}? [y/N]",
                              "n").lower() not in ("y", "yes"):
        print("  nothing sent.")
        return 0
    summary = (f"Sends setAgentWallet to the ERC-8004 identity registry on {built['chain_label']}, for identity "
               f"#{built['agent_id']}. Pick the account that owns the identity ({built['owner']}) in your wallet; the "
               f"registry refuses any other. It names {name}'s own wallet as where the identity is paid; ownership "
               f"and funds do not change. The call carries the registry's own consent message (a format the registry "
               f"defines), already signed by {name}'s wallet; your wallet shows the call as raw data, this page shows it decoded. The consent "
               f"is valid for about {built['valid_seconds'] // 60} minutes. Emporia sends nothing itself; your wallet "
               "shows the gas" + (" (test ETH)." if "testnet" in built["chain_label"] else "."))
    try:
        page = browser_sign.SendPage({"chain_id": built["chain_id"], "to": built["registry"], "data": built["calldata"],
                                      "function": "setAgentWallet(uint256 agentId, address newWallet, uint256 deadline, "
                                                  "bytes signature)"},
                                     chain_label=built["chain_label"], agent_name=name, summary=summary, port=port,
                                     listen=addr, timeout=float(built["valid_seconds"])).start()   # ends with the consent
    except browser_sign.BrowserSignError as e:
        print(f"agent set-8004-wallet: {e}")
        return 1
    _announce_page(page, chain_label=built["chain_label"], registry=built["registry"], what="transaction",
                   lead=f"Open this page in the browser that has your wallet. It works while this command runs "
                   f"(up to {int(page.timeout // 60)} min, as long as the consent in it holds); Ctrl-C cancels.")
    try:
        got = page.wait()
    except browser_sign.BrowserSignError as e:
        print(f"agent set-8004-wallet: {e}; nothing sent")
        return 1
    except KeyboardInterrupt:
        page.close()
        print("\n  cancelled; nothing sent (if your wallet already sent it, use --tx with its hash).")
        return 1
    print(f"  Your wallet sent transaction {got['tx_hash']} from {got['account']}.")
    return _wallet_after(t, name, chain, built["agent_id"], got["tx_hash"], waiting=True, wait=wait)


def _wallet_after(t: _Target, name: str, chain: str, agent_id: str, tx: str, *, waiting: bool = False,
                  wait: bool = False) -> int:
    """Read the receipt and `getAgentWallet` at the safe block, and say whether it is the agent's own wallet. With `wait`,
    read again every 15 seconds until the receipt is at the safe block."""
    resume = f"emporia agent set-8004-wallet {name} --chain {_cmd_chain(chain)} --agent-id {agent_id} --tx {tx}"

    def ask() -> dict[str, Any]:
        return _chain_run(t, "confirm", chain, agent_id, tx, mod="chain_wallet")

    if wait:
        out = _wait_for(ask, lambda o: not _not_yet(o), "the transaction to be safe", resume + " --wait")
        if out is None:
            return 1
    else:
        out = ask()
    if not out.get("success"):
        print(f"agent set-8004-wallet: {out.get('error')}")
        if waiting:
            print(f"  Once it is mined: {resume}")
        return 1
    print(f"  ERC-8004 #{out['agent_id']} on {out['chain_label']}: its agent wallet is now {out['agent_wallet']} "
          f"(block {out['block_number']}, read at the safe block {out['read_at_block']}).")
    if out["is_own_wallet"]:
        print(f"  That is {name}'s own wallet: the identity is paid at its own wallet. (This says where it is paid; it "
              "is not proof of control.)")
        from emporia import networks
        n = networks.NETWORKS.get(str(out.get("chain") or ""))
        if n and n.explorer:
            print(f"  Transaction: {n.explorer}{tx}")
        return 0
    print(f"  That is not {name}'s own wallet ({out['own_wallet'] or 'none here'}).")
    return 1


def unlink_8004(name: str, chain: str, agent_id: str) -> int:
    """`emporia agent unlink-8004 NAME --chain C --agent-id N`: a signed removal; the marker stays."""
    t = _link_target("unlink-8004", name)
    if t is None:
        return 1
    if _refuse_in_agent_session("unlink-8004"):
        return 1
    if not chain or not str(agent_id or "").strip():
        print("agent unlink-8004: --chain and --agent-id N are required")
        return 1
    out = _chain_run(t, "unlink", chain, agent_id)
    if not out.get("success"):
        print(f"agent unlink-8004: {out.get('error')}")
        return 1
    print(f"  ERC-8004 #{agent_id} is off {name}'s card; a signed removal marker stays. What was already published "
          "(the wallet address, in relay copies and the transparency log) is not erased.")
    return 0
