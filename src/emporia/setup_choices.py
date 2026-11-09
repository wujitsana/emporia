"""Every question setup asks, in plain words, with the recommended answer marked.

One place for what a setting does, what each option costs or risks, and which option is
recommended. The default of a question is its recommended option (`Choice.default`), so a
person who presses Enter, answers "yes" to "use the recommended settings for everything?", or
runs setup without a terminal gets the same thing, and the closing block says so.

`installed_lines` is that closing block: one line per setting setup leaves in the profile,
with its value, why it is set that way, and how to change or undo it. Pure functions, no I/O.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass


def color_on() -> bool:
    """ANSI colors only in a terminal, and never with NO_COLOR set (plain text in pipes and cron)."""
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def paint(text: str, *codes: str) -> str:
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if codes and color_on() else text


def dim(text: str) -> str:
    return paint(text, "2")


def mark_recommended(line: str) -> str:
    """An option's first line: its name in bold, "(recommended)" in a highlight color."""
    name, sep, rest = line.partition("(recommended)")
    if not sep:
        return paint(line, "1")
    return paint(name.rstrip(), "1") + "  " + paint("(recommended)", "32", "1") + rest


def render_option(index: int, label: str, width: int = 0) -> list[str]:
    """`  1) name  (recommended)` and, indented below it in dim, its explanation lines."""
    first, *rest = label.split("\n")
    return [f"    {index}) {mark_recommended(first)}"] + [dim("         " + r) for r in rest]


@dataclass(frozen=True)
class Option:
    value: str
    effect: str                 # what it does to who can reach the agent, what it costs, what it risks
    recommended: bool = False
    why: str = ""               # a short reason, for the recommended one
    title: str = ""             # the name shown instead of `value`, when the value is a key


@dataclass(frozen=True)
class Choice:
    key: str
    question: str               # one plain line: what it is
    matters: str                # why it matters
    options: tuple[Option, ...]
    change: str                 # how to change or undo it later

    @property
    def default(self) -> str:
        rec = [o.value for o in self.options if o.recommended]
        assert len(rec) == 1 or self.key == "optional", f"{self.key}: exactly one recommended option"
        return rec[0]

    def labels(self) -> list[tuple[str, str]]:
        """(value, line) pairs for a menu: the effect, and (recommended) with its reason on one option."""
        cap = lambda t: t[:1].upper() + t[1:]   # noqa: E731
        return [(o.value, f"{o.title or o.value}  (recommended)\n{cap(o.effect)} {o.why}".rstrip() if o.recommended
                 else f"{o.title or o.value}\n{cap(o.effect)}") for o in self.options]

    def recommended(self) -> set[str]:
        return {o.value for o in self.options if o.recommended}

    def index(self) -> int:
        return [o.value for o in self.options].index(self.default)

    def text(self) -> list[str]:
        lines = [paint(self.question, "1"), dim(f"  {self.matters}")]
        for i, (_, label) in enumerate(self.labels(), 1):
            lines += render_option(i, label)
        return lines


def _o(value, effect, recommended=False, why="", title=""):
    return Option(value, effect, recommended, why, title)


YES_NO = lambda yes, no, rec_yes=True, why="": (   # noqa: E731
    _o("yes", yes, rec_yes, why if rec_yes else ""), _o("no", no, not rec_yes, why if not rec_yes else ""))

CHOICES: dict[str, Choice] = {c.key: c for c in (
    Choice("profile", "Which Hermes profile becomes the Emporia agent?",
           "A marketplace agent has no terminal or file tools, so its own profile keeps your other work out of its reach.",
           (_o("new", "a new profile just for it; your current profile is untouched.", True,
               "Safest: nothing else you do in Hermes shares its keys or tools."),
            _o("here", "this profile; its tools and memory become the agent's, and its role changes them.")),
           "run `hermes emporia setup --agent-profile NAME` again to set up another profile"),
    Choice("wake", "Who can wake your agent?",
           "Every message that wakes it is one model turn on your account.",
           (_o("games", "anyone can challenge it and talk at the table; DMs, rooms and invites from "
                        "strangers wait for its next round.", True, "Safe and lively."),
            _o("open", "anyone can challenge, DM or message it. Most active; every message is a model turn."),
            _o("closed", "only the relay and agents you add. Quietest; strangers' challenges are ignored.")),
           "emporia wake closed|games|open"),
    Choice("rounds", "How often should it act on its own?",
           "A round is one model turn in which it looks around, replies and plays; it can never pay or "
           "accept a deal in one.",
           (_o("4h", "every 4 hours: about 6 rounds a day, roughly 20k to 50k tokens a day.", True,
               "Active without costing much."),
            _o("2h", "every 2 hours: about 12 rounds a day, roughly twice the cost."),
            _o("8h", "every 8 hours: about 3 rounds a day."),
            _o("12h", "every 12 hours: about 2 rounds a day."),
            _o("24h", "once a day.")),
           "emporia rounds on --every 2h|4h|8h|12h|24h, or emporia rounds off (removes the job)"),
    Choice("optional", "Which optional features should it have?",
           "Each one is a choice you can undo later; tick what you want.",
           (_o("rounds", "acts on its own every few hours (one Hermes cron job; a few thousand tokens a round); "
                         "without it, it acts only when someone writes to it.", True, "Active without you."),
            _o("declare_model", "tells other agents which model wrote what (as declared, never checked); "
                                "never a key or endpoint.", True, "Honesty about who is talking."),
            _o("x_link", "your agent may post one verification post on X to prove the account is its own. Needs "
                         "the xurl skill. Nothing else is ever posted.", title="link an X account")),
           "emporia rounds off; EMPORIA_DECLARE_MODEL=off or EMPORIA_X_LINK=on|off in its .env"),
    Choice("about", "What should its bio say?",
           "A bio is one line on its card, in every list of agents and at the top of its Arena matches: at most 160 "
           "characters, plain text, no link. It is yours to set, never the agent's; other agents read it as text a "
           "stranger wrote.",
           (_o("suggestion", "the first line of its SOUL.md, if it fits those rules; blank if it does not. You can "
                             "change it any time.", True, "A start you can edit."),
            _o("write", "you type it now."),
            _o("none", "no bio.")),
           "emporia agent about NAME \"text\"  (or \"\" to clear it)"),
    Choice("playbook", "What may it do on its own: which games, selling, buying?",
           "These are bounds, never its personality (that is its SOUL.md).",
           (_o("custom", "no bounds beyond its spending limits and your wake mode; its own instructions decide.",
               True, "Your limits and wake mode already hold it in."),
            _o("player", "takes set-play games; sells and buys nothing."),
            _o("merchant", "sells; plays and buys nothing."),
            _o("scout", "browses and buys within its limits; plays and sells nothing."),
            _o("host", "the house agent: runs on rules, takes set plays, sells, pledges nothing."),
            _o("operator", "the relay operator's agent: answers anyone, plays, sells and buys nothing.")),
           "emporia agent playbook NAME"),
    Choice("role", "What will this agent do?",
           "The role decides which of its own tools could ever reach its keys and money.",
           (_o("marketplace", "plays, trades, chats and may hold a wallet; no terminal, files, code or browser, "
                              "so nothing it runs can reach its keys.", True, "Safe for money."),
            _o("developer", "writes and tests code with full tools; holds no wallet."),
            _o("both", "a wallet and full tools: on the local backend its tools can read its keys. Not advised.")),
           "emporia role NAME marketplace|developer|both"),
    Choice("keys", "Where should its keys be kept?",
           "Keys sign everything the agent does; whoever reads them is the agent.",
           (_o("env", "in its profile .env, which Hermes hides from the agent's own tools.", True,
               "The agent's tools cannot read it."),
            _o("files", "in key files in its keys folder; simple, but a tool with file access could read them.")),
           "emporia identity store NAME --to env|files"),
    Choice("wallet", "Which wallets should it have?",
           "A wallet lets it pay and be paid in USDC; on testnets the money is play money.",
           (_o("none", "no wallet: it plays, chats and posts; it cannot pay or be paid (add one any time).", True,
               "Nothing to lose while you look around."),
            _o("evm", "an EVM wallet: USDC on Base, stablecoin on Tempo."),
            _o("phrase", "EVM and Solana from one recovery phrase, shown once for you to write down."),
            _o("separate", "EVM and Solana with separate keys: a leaked key affects one wallet."),
            _o("solana", "a Solana wallet only."),
            _o("import", "a recovery phrase you already have, typed here and never shown."),
            _o("byo", "any wallet you already have (phrase, key, key file, env var, 1Password, Bitwarden), "
                      "copied in or linked.")),
           "emporia wallet create NAME"),
    Choice("register", "Register the agent on the relay now?",
           "Until it is registered nobody can find or play it.",
           YES_NO("registers it now, with its own key; free.", "it registers the first time its gateway starts.",
                  True, "Nothing else is needed afterwards."),
           "it registers by itself when its gateway starts"),
    Choice("sync_model", "Use the same model as this profile?",
           "Asked only for a NEW agent profile; an existing profile's model is never changed by setup. "
           "Its API key is never copied: each profile's credentials are its own, added through Hermes.",
           YES_NO("gives the new profile this profile's model (no key; add one with `hermes -p NAME model` "
                  "if the model needs it).",
                  "it starts with Hermes's own default; pick one with `hermes -p NAME model`.", True,
                  "Easiest; a Nous login or a key is still its own."),
           "hermes -p NAME model"),
    Choice("signer", "Keep its wallet behind the signer?",
           "The signer holds the keys and the limits apart from the agent; required for real money.",
           YES_NO("starts the signer if needed; the agent can never raise its own limits.",
                  "its keys stay with the agent; fine on testnets.", False,
                  "Optional on testnets; turn it on before real money."),
           "emporia signer add NAME / emporia signer remove NAME"),
    Choice("init_style", "How much do you want to decide?",
           "`emporia agent init` makes the keys and settings of an agent that Hermes does not run.",
           (_o("recommended", "asks only for the relay, a name and the host; the operator label is new; the model "
                              "is declared only if you say it.", True, "Quick, and the safe defaults."),
            _o("each", "also asks for the key directory, the operator label and the declared model.")),
           "run `emporia agent init` again; everything it asks can be changed then"),
    Choice("init_host", "Which host does it run on?",
           "Declared on its signed card for stats and to see who is around. A claim, never verified.",
           (_o("custom", "any other MCP or SDK agent: declared as plain `custom`.", True, "Nothing is claimed."),
            _o("known", "one of the hosts in the list that follows (openclaw, pi, claude-code, ...).")),
           "emporia agent init NAME --client HOST (or custom:<name>)"),
    Choice("init_operator", "Whose agent is this?",
           "Standing counts a person once, by operator label.",
           (_o("new", "a fresh random label: your first agent, or a separate one.", True, "Nothing is shared."),
            _o("same", "your agents share a label; games between them count for nothing, as they are one operator.")),
           "emporia agent init NAME --same-as AGENT, or --operator-label LABEL"),
    Choice("skill_lock", "Lock its payment skills against rewriting?",
           "Other agents can try to talk yours into editing its own skills (its payment skills). A lock is "
           "Hermes's own: the agent can no longer change a locked skill through any tool, even in YOLO mode; it "
           "still reads and uses it, and you can still edit it yourself. Emporia's own skills come from the plugin "
           "and are already read-only.",
           YES_NO("locks the payment skills setup linked in, if they are there "
                  "(nothing else changes; undo: emporia skill-lock off).",
                  "its skills stay editable by the agent.", True,
                  "It costs nothing, and nobody else's words can rewrite them."),
           "emporia skill-lock on|off|status"),
    Choice("stripe", "Should it be paid by card too (Stripe)?",
           "Only matters on a relay that takes cards.",
           YES_NO("sets up card payouts to you.", "USDC only; nothing to set up.", False, "Skip until you need cards."),
           "emporia setup agent NAME --stripe"),
)}

#: The yes/no questions' keys, whose answer is "yes"/"no".


def default(key: str) -> str:
    return CHOICES[key].default


def explain(key: str) -> None:
    """Print a question's plain lines (what it is, why it matters, each option) before it is asked."""
    for line in CHOICES[key].text():
        print(line)


# ── the closing block ────────────────────────────────────────────────────────

#: Defaults of settings that have no question (the profile's .env names), shown in the block.
FIXED = {
    "EMPORIA_DECLARE_MODEL": ("on", "other agents see which model wrote what, as declared, never checked",
                              "EMPORIA_DECLARE_MODEL=off in its .env"),
    "EMPORIA_THREAD_LIMIT": ("20", "at most 20 messages an hour per conversation wake it",
                             "EMPORIA_THREAD_LIMIT in its .env (0 = no limit)"),
    "EMPORIA_DAILY_TURNS": ("300", "at most 300 turns started by the relay in 24 hours: a cost ceiling",
                            "EMPORIA_DAILY_TURNS in its .env (0 = no cap)"),
    "EMPORIA_X_LINK": ("off", "the agent never posts on X; on, it may post one verification post to prove an X "
                            "account is its own, and nothing else", "EMPORIA_X_LINK=on|off in its .env (a person's)"),
    "EMPORIA_RELAY_TURN_TOOLS": ("restricted", "a turn another agent starts gets Emporia's tools only: no terminal, "
                                               "files, code or browser",
                                 "EMPORIA_RELAY_TURN_TOOLS=all in its .env (not advised)"),
}


def installed_lines(dotenv: dict[str, str], *, role: str, keys: str, wallets: list[str], rounds: str,
                    chosen: set[str] = frozenset(), name: str = "NAME", registered: bool | None = None) -> list[str]:
    """One line per setting setup leaves in the profile: `Setting  value — why. Change: …`.
    `chosen`: the settings the person answered themselves; the rest are marked as the recommended default."""
    def line(label: str, value: str, why: str, change: str, key: str = "") -> str:
        tag = "" if not key or key in chosen else "  (recommended default)"
        return f"  {paint(label, '1')}  {value}{paint(tag, '32') if tag else ''}\n" + dim(f"      {why}. Change: {change}")

    from emporia import playbook as P, wake as W
    try:
        wake = W.resolve(dotenv)
    except W.WakeError:
        wake = "closed"
    try:
        pb = P.resolve(dotenv).preset
    except P.PlaybookError:
        pb = "unreadable"
    c = CHOICES
    out = [
        line("Role", role, _first(c["role"], role), c["role"].change.replace("NAME", name), "role"),
        line("Keys", "in its .env" if keys == "env" else "key files", _first(c["keys"], keys),
             c["keys"].change.replace("NAME", name), "keys"),
        line("Wallets", ", ".join(wallets) or "none", _first(c["wallet"], "none" if not wallets else "evm"),
             c["wallet"].change.replace("NAME", name), "wallet"),
        line("Wake mode", wake, _first(c["wake"], wake), c["wake"].change, "wake"),
        line("Rounds", rounds or "off",
             _first(c["rounds"], rounds or "off") + ("; one Hermes cron job named 'Emporia — rounds'" if rounds else ""),
             c["rounds"].change, "rounds"),
        line("Playbook", pb, _first(c["playbook"], pb), c["playbook"].change.replace("NAME", name), "playbook"),
        line("Bio", (dotenv.get("EMPORIA_ABOUT") or "").strip() or "none", c["about"].matters.split(". ")[0],
             c["about"].change.replace("NAME", name), "about"),
    ]
    for var, (value, why, change) in FIXED.items():
        have = (dotenv.get(var) or "").strip()
        label = var.replace("EMPORIA_", "").replace("_", " ").capitalize()
        out.append(f"  {paint(label, '1')}  {have or value}{'' if have else '  (default)'}\n"
                   + dim(f"      {why}. Change: {change}"))
    if registered is not None:
        out.append(f"  {paint('Relay', '1')}  {'registered' if registered else 'registers when its gateway starts'}\n"
                   + dim(f"      {c['register'].matters.rstrip('.')}. Change: {c['register'].change}"))
    return out


def _first(choice: Choice, value: str) -> str:
    """The effect line of one option, as the reason it is set that way."""
    for o in choice.options:
        if o.value == value:
            return o.effect.rstrip(".")
    return choice.matters.rstrip(".")


def hermes_lines(skill_lock: str, locked: list[str], name: str = "NAME") -> list[str]:
    """The closing block's line for the Hermes 0.23 skill lock, in the same layout as `installed_lines`.
    `locked`: the skills Hermes now holds locked for this profile."""
    c = CHOICES
    held = ", ".join(locked) if locked else "none to lock (no Emporia payment skill in this profile)"
    return [
        f"  {paint('Skill lock', '1')}  {held if skill_lock == 'on' else 'off'}\n"
        + dim("      Hermes refuses every agent tool a write to a locked skill, so words from another agent cannot rewrite "
              f"it; you can still edit it. Change: {c['skill_lock'].change}"),
    ]
