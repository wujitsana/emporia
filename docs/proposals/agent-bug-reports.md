# Proposal: agents write bug reports; a person files them

Status: **design only, not built** (2026-10-28). It comes from ROADMAP.md § Before publishing.

## The goal

When an agent hits something that looks wrong, it writes down what it saw, in a form a person
can read, check and, if it holds up, turn into a GitHub issue. The agent never posts anything
itself.

## Why agents never post

- **No token in an agent profile.** Posting needs a GitHub token. An agent profile is where
  prompt-injected text and model mistakes land; a token there would be one more secret an agent
  could be talked into using. Profiles hold no such token and should not start to.
- **Agents are confident and often wrong.** The first real report (chess, 10-28) had both agents
  blame a "relay auto-player". The cause was each agent racing its own game conversation (P226);
  the relay was fine. A public issue saying otherwise would have cost a reader time and made the
  project look worse than it is. A person (or the coding agent working for them) checks first.
- **Public means public.** Session details, room ids and wallet addresses do not belong in a
  public issue unless the person decides so.

## Where a report goes

A local file in the agent's profile, which the person's own tools read:

- `reports/YYYY-MM-DD-HHMMSS-<slug>.json` under the agent's Emporia folder (beside its evidence
  files), mode 0600, written by a new MCP tool, `report_problem`.
- Later, the dashboard's Emporia tab gets a read-only "Reports" list for the profile it is open
  in. It stays read-only: the dashboard makes no writes (P234).
- `emporia reports` (in the person's terminal) lists them across profiles, shows one, and marks
  one "dismissed" or "drafted".

The relay is not involved: a report is not a protocol message and is never sent to any relay.

## What a report contains

| Field | Notes |
|---|---|
| `what_was_seen` | The agent's plain description of the symptom, in its own words, before any theory. |
| `expected` | What it thought would happen. |
| `ids` | Session ids, room ids, listing ids, conversation ids, and the relay host. |
| `steps` | The tool calls it made, in order, by name, with the arguments' shape and not their values. |
| `evidence` | Error text as the tool returned it, and the relay version from `/health`. |
| `guess` | The agent's theory of the cause. Always stored under this name and shown as "the agent's guess, unchecked". Optional. |
| `agent`, `time`, `emporia_version` | Who wrote it and when. |

The structure keeps observation apart from guess. In the chess case the "what was seen" was
accurate (moves out of order); the "relay auto-player" belonged under `guess`.

## What is redacted

The tool redacts before writing, in code, not by asking the model:

- Wallet addresses (EVM, Solana) become `[wallet]`.
- Anything shaped like a key, token, seed phrase or bearer credential is removed, and the report
  is refused if the scan finds a likely secret it cannot cleanly remove. (The guardrails' secret
  scan is reused.)
- Message contents (DMs, room messages, game talk) are never included. The agent may say a
  message "was received" and give its id.
- URLs lose their query strings.
- A person may add any of these back when reviewing, by editing the draft. Redaction is the
  default, not a ceiling.

## How a report becomes an issue

1. The person (or their coding agent) opens `emporia reports show ID`.
2. They check it against the code and logs. Is the symptom real, is it already known, does the
   guess hold up?
3. `emporia reports draft ID` writes an issue draft in Markdown: title, what happened, steps,
   expected, environment, and the guess under a clearly marked heading if the reviewer keeps it.
   The person reads it, adds what they choose, and posts it themselves with their own account.
   Nothing in Emporia holds a GitHub token.
4. The report is marked "drafted", with the issue number if the person enters it.

## Abuse and noise

- A cap per agent per day (a small number, in code), so a looping agent cannot fill the disk.
- A report is data to the reviewer, not instructions. The dashboard shows it escaped, and a coding
  agent reading one treats it as untrusted.
- Duplicates are folded by a hash of the symptom and ids, with a count.

## What it does not solve

It cannot tell whether the agent is right. It makes a wrong guess visible as a guess, and keeps
the cost of checking low.

## The smallest first version

1. One MCP tool, `report_problem(what_was_seen, expected, ids, steps, guess)`, that redacts,
   applies the daily cap and writes the JSON file. (The tool count, README and both skills'
   inventories change with it.)
2. `emporia reports` with `list` and `show`, in the person's terminal only (blocked from agent
   chats like the other person-only verbs).
3. Skill text telling agents to use it for failures they cannot explain, and to put theories
   under `guess`.

Left for later: the dashboard list, `emporia reports draft`, duplicate folding.
