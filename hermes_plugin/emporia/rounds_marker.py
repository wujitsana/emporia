"""The first line of every rounds prompt, so a hook can tell a round from any other turn.

Self-contained: imports nothing (the Hermes plugin and Emporia's runtime both read it).
`rounds_prompt.md` starts with exactly this line; tests keep the two equal. The number is the
prompt's version.
"""

ROUNDS_MARKER = "[emporia-rounds v1]"
