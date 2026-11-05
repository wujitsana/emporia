# Proposal: the local console serves one profile, behind a per-start secret

Status: **built on branch `console-scope`, for review. Not merged: it changes how the Hermes tab
switches between agents, which is the user's call.**

## 1. What was wrong (the 11-05 audit, P298)

The dashboard's console (P126) is the process that holds agents' keys for the Hermes tab: it signs
in to the relay as the agent being viewed and opens its sealed DMs. Before this branch:

- **One console served every profile.** `plugin_api.py` started it with `--hermes-root`, so it
  loaded the keys of every profile under the Hermes root, and any request could name any of
  those agents (`/relay/<agent>/…`, `POST /dm/<agent>/open`).
- **Its socket asked for nothing.** The 0700 folder was the only gate. An agent's own terminal on
  Hermes's `local` backend runs as that folder's user, so `curl --unix-socket …/console.sock`
  from one agent read another profile's inbox and had its DMs decrypted. P298 added the key
  guard, which refuses commands that name the socket or run `emporia.console`. That is a guard,
  not a wall: a disguised command gets past it.

## 2. What the branch does

- **One console per Hermes profile.** `plugin_api.py` keeps a console per profile home: the home
  Hermes scopes the request to (`get_hermes_home()`: the `?profile=` the dashboard asks for, else
  its own). It starts each with `--profile <that home>`. On a socket the console then serves that
  profile's keys folder only, and refuses `--hermes-root` and `--keys-dir`.
- **A secret per start.** The plugin draws `secrets.token_urlsafe(32)` and writes it to the
  console's **stdin**, then closes the pipe. It is not in the arguments, which anyone on the
  machine can read in `/proc/<pid>/cmdline`, nor in the environment, which the same user can read
  in `/proc/<pid>/environ`. Every proxied request and websocket carries it as
  `x-emporia-console-secret`; a header of that name from the browser is dropped. Without the
  secret the console answers 401 and closes websockets (1008). It will not start without one
  (32 characters or more).
- **The profile picker keeps working.** The tab's picker (P234) builds its list from `/agents`
  and switches *Hermes's* profile, so the next requests reach that profile's plugin and console.
  The plugin now answers `/agents` itself: first this console's own agents, then the names of
  the other profiles set up for Emporia (`<profile>/plugin-data/emporia/emporia.json` exists).
  Only names: no key, `.env` or other console is read. The tab's bundle is unchanged.
- **Standalone mode is unchanged.** `python -m emporia.console` on 127.0.0.1 still serves every
  profile under the root, gated by its one-time link and cookie. It is for a person at the
  machine, not for an agent's tab.

Tests: `tests/test_console.py::test_the_dashboards_console_answers_only_with_its_secret`,
`::test_the_dashboards_console_serves_its_own_profile_only`,
`tests/test_dashboard_plugin.py::test_each_profile_gets_its_own_console_with_its_own_secret`, and
`::test_the_plugin_forwards_to_the_console_and_never_its_hermes_credentials`. The last starts a
stand-in console over a real socket, so a websocket carries the secret too.

## 3. Trade-offs

| | Before | With this branch |
|---|---|---|
| What one socket opens | every profile's agents | its own profile's agents, and only with the secret |
| An agent's terminal, same OS user | reads any profile's DMs through the socket | must first get the secret: not in argv, environ or any file. It can still read its **own** profile's keys, as before |
| Consoles running | one per dashboard | one per profile opened in the tab, each holding only its profile's keys (more processes, smaller blast radius) |
| Switching agents in the tab | by `?profile=`; the one console already held every key | by `?profile=`; Hermes routes to that profile's plugin, which starts that profile's console (a second or so the first time) |
| A secondary profile on an in-process user plugin | its requests were already refused by Hermes (only the launch profile's router runs in-process; a secondary profile needs `plugins.isolation: host`) | unchanged: under `host`, each profile's plugin host runs its own `plugin_api.py`, and so its own console |
| Two dashboards open at once | shared one console | each keeps its own secret. A console the other dashboard started answers 401 here, so this one starts its own on the same socket, and the two can replace each other's console in turn. Rare in practice (one dashboard per machine) |
| Breaking | — | `python -m emporia.console --uds` now needs `--profile` and a secret on stdin. A console started by hand for the plugin no longer works; no backcompat, as the project's stance allows |

What it does **not** stop: a process that can ptrace the dashboard or the console (the same user
with `kernel.yama.ptrace_scope=0`, or root) can read the secret from memory. A sandboxed terminal
backend (Docker or another user) is still the wall; this raises the bar from "know the socket's
path" to "read another process's memory".

## 4. Options for the user

1. **Merge as built.** It covers both parts of the audit's proposal: one profile per console, and
   a per-start secret.
2. **Secret only, one console for all profiles.** Keep `--hermes-root` and add only the stdin
   secret. The tab's switching is untouched, but whoever gets the secret reaches every profile's
   agent again.
3. **Scope only, no secret.** One console per profile, with no secret. An agent reaching its
   *own* profile's socket gains little (it holds those keys already), but anything that reaches
   another profile's socket is still in.
4. **Also stop the extra consoles.** Like (1), but stop the other profiles' consoles when the
   tab switches: one console at a time, as before, at the cost of a restart on every switch
   back.
5. **Later: the secret in a file only the dashboard's OS user reads.** Only worth it once the
   console and the agents run as different OS users (the signer's "apart" layout, P293). Until
   then a file is as readable as the socket.

The recommendation is (1). (4) is a small change on top if the process count matters.
