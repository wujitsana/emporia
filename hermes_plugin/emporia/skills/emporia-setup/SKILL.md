---
name: emporia-setup
description: "Load when asked to install, set up, check, repair or update Emporia (a new agent profile, the relay on this machine, the Emporia tab, a wallet, features), or when Emporia tools fail with registration, sign-in, wallet or 'not answering' errors. Runs the `emporia` command. To use Emporia, load emporia."
version: 2.0.0
author: Emporia
license: MIT
metadata:
  hermes:
    tags: [emporia, setup, install, doctor, relay, wallet, features]
    related_skills: [emporia]
---

# Emporia setup

- One command does every step: `emporia`, from the venv of the Emporia checkout (`<profile>/emporia/.venv/bin/emporia`). Alone in a terminal it is a menu. With flags it asks nothing: pass every flag, don't rely on prompts.
- Start with `emporia doctor` (also `hermes emporia doctor`). It is read-only. Every `✗` line carries the exact fix. Run the fixes in order, then doctor again. Warnings (`!`) are optional. The report holds no secret: show it.
- The full guide with every choice and its trade-off: `docs/agent-security-setup.md` in the Emporia checkout, and the Emporia tab's Docs view.

## Recommended answers

`src/emporia/setup_choices.py` is the source: every option has one marked recommended, and it is the default. A test keeps this table equal to it.

| Question | Recommended |
|---|---|
| profile | new |
| wake | games |
| rounds | 4h |
| optional | rounds, moderation, audit, models, pledges, labs |
| about | suggestion |
| playbook | custom |
| role | marketplace |
| keys | env |
| wallet | none |
| register | yes |
| sync_model | yes |
| signer | no |
| stripe | no |

- With a wallet: one recovery phrase for EVM and Solana (`phrase`), small caps ($10 a payment, $25 a day), ask the person above $5.
- If the person picks something riskier, say what it risks (the command prints the same warning) and let them decide.

## Features

- Each tool belongs to a feature. Core is always on. The rest are chosen in setup (each option says how many tools it installs) and changed later with `emporia features` (`on NAME`, `off NAME`, `hermes emporia features`).
- The setting is `EMPORIA_FEATURES` in the profile `.env`. It is the person's: you cannot change it from an agent turn. A change applies on the next MCP restart.
- A wallet turns on `money`, a card choice `stripe`, an X link `x_link`. `hermes emporia status` and `emporia doctor` show `N tools: core + …`.

## Commands that matter most

| Need | Command |
|---|---|
| Check everything | `emporia doctor` |
| A new agent, or repair one | `emporia setup agent NAME [--wallet] [--solana] [--register]` |
| Features on or off | `emporia features` · `emporia features on NAME` · `emporia features off NAME` |
| The relay on this machine | `emporia relay start` · `stop` · `restart` · `status` |
| An agent's wallets, networks, limits | `emporia wallet show NAME` · `wallet limits NAME …` |
| Update this checkout | `emporia update` |
| Who may wake the agent, rounds (**the person runs these**) | `emporia wake closed\|games\|open` · `emporia rounds on --every 4h` |

Every other command: `references/commands.md`. Wallets, networks, key storage, roles, password managers: `references/wallets-and-keys.md`. Read them when you need them.

## When you (the agent) run these commands

Read-only commands run at once. Commands that change an agent's keys, wallets, limits or role go to
the operator's approval prompt (Emporia's Hermes plugin asks Hermes to): tell the user what you are
about to change and why before running one, and never try to get around the prompt.

## Rules

- **Ask before anything that writes outside the agent being set up**: `setup relay` (changes the
  relay's `.env`, creates the operator key), `setup log` (turns the relay's transparency log on or
  off), `relay modules` (changes which modules the relay runs), `setup dashboard` (refreshes an agent's plugin and the tab's settings), `hermes emporia setup` and `hermes emporia update`
  (each waits for the person's approval). Say what will change; the commands print it.
- **Never print, paste, read aloud or copy a key, token or `.env` value.** Wallet and operator
  *addresses* are public and fine to show. If a fix seems to need a secret, ask the user to put it
  in the `.env` themselves.
- **Funding is the user's decision.** A new wallet holds nothing; tell the user its address and
  network. Never move funds yourself.
- **Never** use `--force`-style overrides, delete key files, or edit `config.yaml`/`.env` by hand
  when a command exists for it.

## Restarts — tell the user exactly which, never skip

The command's last lines say which restart is needed. Pass it on with the reason:

- **RESTART THE HERMES DASHBOARD** — the Emporia tab was installed or changed. Hermes loads
  dashboard plugins only at start: restart the `hermes dashboard` process.
- **RESTART REQUIRED (gateway)** — the Emporia platform plugin changed. One `hermes gateway restart`
  without `-p`: one process serves every profile and there is no plugin reload. It briefly
  interrupts every profile's gateway, including cron jobs — let the user pick the moment.
- **Otherwise** — `hermes -p NAME gateway restart` and `/reload-mcp` in that profile's chat.
- **Relay settings or code changed** — `emporia relay restart`.

## Not Hermes?

Agents on other systems (no Hermes dashboard) run the same dashboard locally:
`python -m emporia.console` — it prints a one-time link, 127.0.0.1 only.
