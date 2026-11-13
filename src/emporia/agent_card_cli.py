"""`emporia agent about | x-claim | x-link | link-8004 | unlink-8004`: the card's bio and links, from a person's terminal.

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

Works for a Hermes agent profile and for an agent made with `emporia agent init` (its key directory).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


def _result(r: subprocess.CompletedProcess) -> dict[str, Any]:
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"success": False, "error": (r.stderr or r.stdout).strip()[-300:] or "no answer"}


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
        print(f"agent about: no agent {name!r} here (a Hermes agent profile, or one made with `emporia agent init`)")
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
        print("  Saved. The relay's card gets it the next time the agent registers (it does at start"
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



def _chain_run(t: _Target, fn: str, *args: Any) -> dict[str, Any]:
    """`chain_link.<fn>(*args)` as the agent; the answer, or {"error": ...}."""
    argtext = ", ".join(repr(a) for a in args)
    return _result(t.run("import json\nfrom emporia import chain_link as c\n"
                         "try:\n"
                         f"    out = c.{fn}({argtext})\n"
                         "    print(json.dumps({'success': True, **out}, default=str))\n"
                         "except c.LinkError as e:\n"
                         "    print(json.dumps({'success': False, 'error': str(e)}))\n"))


def _link_target(cmd: str, name: str) -> "_Target | None":
    t = _Target(name)
    if not t.found:
        print(f"agent {cmd}: no agent {name!r} here (a Hermes agent profile, or one made with `emporia agent init`)")
        return None
    if why := t.unattended():
        print(f"agent {cmd}: a {why} turn is running for {name}: an ERC-8004 link is a person's to change "
              "(it publishes a wallet address). Run it yourself, when no turn is live.")
        return None
    return t


def link_8004(name: str, chain: str, agent_id: str, signer: str = "", signature: str = "",
              issued_at: int = 0, yes: bool = False) -> int:
    """`emporia agent link-8004 NAME --chain C --agent-id N [--signer 0x..] [--signature 0x.. --issued-at T]`."""
    t = _link_target("link-8004", name)
    if t is None:
        return 1
    if not chain or not str(agent_id or "").strip():
        print("agent link-8004: --chain (e.g. base, base-sepolia, robinhood) and --agent-id N are required")
        return 1
    if not signature:
        out = _chain_run(t, "ask", chain, agent_id, signer)
        if not out.get("success"):
            print(f"agent link-8004: {out.get('error')}")
            return 1
        _print_ask(name, out)
        return 0
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


def chain_note(chain: str) -> str:
    from emporia import networks
    c = networks.resolve(chain)
    return f"chain: {networks.label(c)}"


def _wrap_privacy() -> str:
    from emporia import chain_link
    return chain_link.PRIVACY_NOTE


def _print_ask(name: str, out: dict[str, Any]) -> None:
    print(f"  ERC-8004 #{out['agent_id']} on {out['chain_label']}, read at block {out['block_number']}")
    print(f"    owner:   {out['owner']}")
    if out.get("binding"):
        print(f"    binding: {json.dumps(out['binding'], sort_keys=True)}")
    print(f"    signer:  {out['signer']}")
    if out["tier"]:
        print(f"    tier:    {out['tier']} ({out['tier_wording']}): what the card will say once you sign")
    else:
        print(f"    tier:    not proven: {out.get('reason') or 'the chain does not back this signer'}")
    print(f"\n  {out['privacy']}\n")
    print("  Sign this typed message (EIP-712) in the wallet of the signer, e.g. save it as msg.json and run\n"
          "  `cast wallet sign --data --from-file msg.json --private-key ...` or paste it into a wallet that\n"
          "  signs typed data. Nothing goes on-chain; it moves no funds and grants no permission:\n")
    print(json.dumps(out["typed_data"], indent=2))
    print(f"\n  Then record it: emporia agent link-8004 {name} --chain {_short(out['chain'])} "
          f"--agent-id {out['agent_id']} --signer {out['signer']} --issued-at {out['issued_at']} --signature 0x...")


def _short(caip2: str) -> str:
    from emporia import chain_link
    return next((k for k, v in chain_link.chains().items() if v == caip2), caip2)


def unlink_8004(name: str, chain: str, agent_id: str) -> int:
    """`emporia agent unlink-8004 NAME --chain C --agent-id N`: a signed removal; the marker stays."""
    t = _link_target("unlink-8004", name)
    if t is None:
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
