"""The first line of the rounds prompt (the scheduled turn that lets an agent look around on its own).

relay_turn.py recognises a rounds turn by this line: a cron-started turn that begins with it is an
*autonomous* turn, treated like one a relay event started. The agent-activation branch exports the same
constant from this same file name, so the merge keeps one definition. Nothing here imports Emporia.
"""

ROUNDS_MARKER = "[emporia-rounds v1]"
