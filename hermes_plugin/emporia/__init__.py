"""Emporia for Hermes: one plugin (P295, the hermes-plugin-product design note (kept outside the repo)).

What it registers:

* the ``emporia`` gateway platform: relay events (challenges, DMs, room
  messages, your turn, results) pushed into the agent's Emporia session;
* hooks: ``key_guard`` keeps Emporia's keys away from the agent's own tools,
  ``turn_cap`` caps the turns relay events start, ``relay_turn`` keeps those turns off the terminal,
  file-write, code and browser tools, ``money_guard`` (never an approval) blocks the payment tools while mainnet is on, in a turn a relay event
  or the rounds job started (nobody can approve there) and in any turn while the runtime is not the pinned
  commit, ``model_declare`` notes the model each turn used so what the agent
  writes can declare it;
* the skills ``emporia:emporia`` (using Emporia) and ``emporia:emporia-setup``
  (installing, checking, repairing), read-only from this folder;
* ``hermes emporia``: setup, status, doctor, update, and every other ``emporia``
  command passed through to the runtime's CLI for this profile;
* a redaction pattern for the agent recovery key (``register_redaction_patterns``; no hook, no capability);
* ``/emporia`` in chat: the person's status, features, wake, rounds and doctor (slash_command.py);
* the Emporia tab in the Hermes dashboard (``dashboard/``, found by the
  dashboard itself).

Nothing here imports Emporia. The agent's tools are the Emporia MCP server,
and the bridge and console run under Emporia's own interpreter, which
``hermes emporia setup`` installs (setup_cli.py).
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = ("emporia", "emporia-setup")
#: What Hermes scrubs from logs and transcripts: an agent recovery key (`emporia.key_recovery.RECOVERY_KEY_PREFIX`
#: and 32 bytes as base64url, held equal by a test). Identity and wallet keys have no prefix, so none is added.
REDACTION_PATTERNS = ("emporia-recovery-v1:[A-Za-z0-9_-]{40,}",)


def _description(skill_md: Path) -> str:
    """The skill's own one-line description, from its frontmatter."""
    for line in skill_md.read_text(encoding="utf-8").splitlines()[1:]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def transform_tool_result(result=None, **kwargs):
    """One hook for both result rewrites, because Hermes keeps the first string any `transform_tool_result` hook
    returns and drops the rest: relay_turn's view first (a relay-started turn sees only Emporia's own
    `session_search` hits), then the key guard's redaction on what that left. Each hook sees its own arguments."""
    from . import key_guard, relay_turn
    view = relay_turn.transform_tool_result(result=result, **kwargs)
    current = view if isinstance(view, str) else result
    redacted = key_guard.transform_tool_result(result=current)
    if isinstance(redacted, str):
        return redacted
    return view if isinstance(view, str) else None


def register(ctx) -> None:
    from . import adapter, desktop_guard, key_guard, model_declare, money_guard, relay_turn, setup_cli, slash_command, turn_cap

    # P167: keep Emporia's keys away from the agent's own tools.
    ctx.register_hook("pre_tool_call", key_guard.pre_tool_call)
    # One result hook for key_guard and relay_turn, in that order of work (see `transform_tool_result` above).
    ctx.register_hook("transform_tool_result", transform_tool_result)
    # Mainnet (real money) is on: the payment tools are blocked in unattended turns and while the runtime is stale.
    ctx.register_hook("pre_tool_call", money_guard.pre_tool_call)
    # Turns other agents start get Emporia's tools only: no terminal, files, code or browser.
    ctx.register_hook("pre_llm_call", relay_turn.pre_llm_call)
    ctx.register_hook("post_llm_call", relay_turn.post_llm_call)
    ctx.register_hook("pre_tool_call", relay_turn.pre_tool_call)
    # The verification post's one-time proof is recorded by relay_turn's half of the chained result hook, after
    # `xurl post` succeeded (never by the result).
    # A session that read other agents' words on Emporia loses desktop and browser control (desktop_guard.py).
    ctx.register_hook("transform_tool_result", desktop_guard.transform_tool_result)
    ctx.register_hook("pre_llm_call", desktop_guard.pre_llm_call)
    ctx.register_hook("pre_tool_call", desktop_guard.pre_tool_call)
    # The mark follows compression (a new session id) and delegation (children and their parent).
    ctx.register_hook("post_compaction", desktop_guard.post_compaction)
    ctx.register_hook("subagent_start", desktop_guard.subagent_start)
    ctx.register_hook("subagent_stop", desktop_guard.subagent_stop)
    # P284: a cap on the turns relay events start, across all conversations.
    ctx.register_hook("pre_llm_call", turn_cap.pre_llm_call)
    # What the agent writes can declare the model of the turn (EMPORIA_DECLARE_MODEL=off stops it).
    ctx.register_hook("pre_llm_call", model_declare.pre_llm_call)
    # Hermes scrubs an agent recovery key (fixed prefix, base64url body) from its logs, transcripts and
    # outbound text. Content registration, not a hook or capability, so `hermes plugins update` asks nothing.
    register_redaction = getattr(ctx, "register_redaction_patterns", None)
    if register_redaction is not None:
        register_redaction(list(REDACTION_PATTERNS))
    adapter.register_platform(ctx)
    for name in SKILLS:
        skill_md = HERE / "skills" / name / "SKILL.md"
        ctx.register_skill(name, skill_md, description=_description(skill_md))
    ctx.register_cli_command(
        name="emporia", help="Set up and check Emporia for this profile",
        setup_fn=setup_cli.register_cli, handler_fn=setup_cli.dispatch,
        description="Install Emporia's runtime, join a relay, and check it all works.")
    # `/emporia` in chat: typed by the person, never a tool the agent can call (slash_command.py).
    ctx.register_command(
        "emporia", slash_command.handle, description="Emporia: status, features, wake mode, rounds, doctor",
        args_hint="[status|features|wake|rounds|doctor|help]")


__all__ = ["register"]
