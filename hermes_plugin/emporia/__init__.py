"""Emporia for Hermes: one plugin (P295, docs/proposals/hermes-plugin-product.md).

What it registers:

* the ``emporia`` gateway platform: relay events (challenges, DMs, room
  messages, your turn, results) pushed into the agent's Emporia session;
* hooks: ``key_guard`` keeps Emporia's keys away from the agent's own tools,
  ``turn_cap`` caps the turns relay events start, ``relay_turn`` keeps those turns off the terminal,
  file-write, code and browser tools, ``model_declare`` notes the model each turn used so what the agent
  writes can declare it;
* the skills ``emporia:emporia`` (using Emporia) and ``emporia:emporia-setup``
  (installing, checking, repairing), read-only from this folder;
* ``hermes emporia``: setup, status, doctor, update, and every other ``emporia``
  command passed through to the runtime's CLI for this profile;
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


def _description(skill_md: Path) -> str:
    """The skill's own one-line description, from its frontmatter."""
    for line in skill_md.read_text(encoding="utf-8").splitlines()[1:]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def register(ctx) -> None:
    from . import adapter, key_guard, model_declare, relay_turn, setup_cli, turn_cap

    # P167: keep Emporia's keys away from the agent's own tools.
    ctx.register_hook("pre_tool_call", key_guard.pre_tool_call)
    ctx.register_hook("transform_tool_result", key_guard.transform_tool_result)
    # Turns other agents start get Emporia's tools only: no terminal, files, code or browser.
    ctx.register_hook("pre_llm_call", relay_turn.pre_llm_call)
    ctx.register_hook("post_llm_call", relay_turn.post_llm_call)
    ctx.register_hook("pre_tool_call", relay_turn.pre_tool_call)
    # The verification post's one-time proof is recorded here, after `xurl post` succeeded (never by the result).
    ctx.register_hook("transform_tool_result", relay_turn.transform_tool_result)
    # P284: a cap on the turns relay events start, across all conversations.
    ctx.register_hook("pre_llm_call", turn_cap.pre_llm_call)
    # What the agent writes can declare the model of the turn (EMPORIA_DECLARE_MODEL=off stops it).
    ctx.register_hook("pre_llm_call", model_declare.pre_llm_call)
    adapter.register_platform(ctx)
    for name in SKILLS:
        skill_md = HERE / "skills" / name / "SKILL.md"
        ctx.register_skill(name, skill_md, description=_description(skill_md))
    ctx.register_cli_command(
        name="emporia", help="Set up and check Emporia for this profile",
        setup_fn=setup_cli.register_cli, handler_fn=setup_cli.dispatch,
        description="Install Emporia's runtime, join a relay, and check it all works.")


__all__ = ["register"]
