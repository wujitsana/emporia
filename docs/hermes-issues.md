# Hermes issues to file

Draft issues for the four Hermes behaviours Emporia works around (docs/HANDOFF.md, "Hermes
behaviours this code works around"). **The person posts these; agents never do.** Check each
against the current Hermes release before posting, and fill in the version.

Each draft is written to stand alone: paste the title and the body.

---

## 1. Dashboard plugins cannot open a websocket in any profile but the launch one

**Title:** Dashboard plugin websockets are refused outside the launch profile ("does not carry websockets yet")

**What happens.** A dashboard plugin that opens a websocket to its own backend route works when
the dashboard is open in the profile Hermes was started with. Switch the dashboard to another
profile and the connection is refused with a message saying the profile "does not carry websockets
yet". Plain HTTP requests from the same plugin work.

**Steps to reproduce.**
1. Start Hermes with profile `a`. Install a dashboard plugin that serves a websocket route.
2. Open the dashboard in profile `a`. The websocket connects.
3. Switch the dashboard to profile `b` (the profile picker).
4. Reload the plugin tab. The websocket is refused.

**What we expected.** The same plugin route answers websockets for whichever profile the
dashboard is showing, as it does for HTTP.

**What we do instead.** The Emporia tab falls back to fetching every 5 seconds when the websocket
is refused (P236). Its live event feed is off in profiles other than the launch one.

**Suggested change.** Carry websockets for every profile, or, if that is not possible yet, say in
the plugin documentation that they work only in the launch profile, and give plugins a way to ask
which case they are in so they can poll without first failing.

---

## 2. Requests to `/api/plugins/*` carry no profile

**Title:** `/api/plugins/*` requests do not say which profile the dashboard is showing

**What happens.** When a dashboard plugin calls its backend at `/api/plugins/<name>/...`, the
request carries no `?profile=` and no header naming the profile selected in the dashboard. The
backend cannot tell which profile's data the person is looking at.

**Steps to reproduce.**
1. Install a dashboard plugin with a backend route that logs the request URL and headers.
2. Open the dashboard in profile `a`, then switch to profile `b`.
3. Call the route from the plugin tab in each. The two requests look the same.

**What we expected.** The profile in use is part of the request: a query parameter, a header, or
a path segment, the same way the rest of the dashboard API is scoped.

**What we do instead.** The plugin's script reads the profile from its own script tag
(`data-hermes-profile`) or from the page URL, and adds it to every call itself (P227, P234). This
works, but each plugin has to rediscover it, and it breaks if either source changes.

**Suggested change.** Add the active profile to plugin API requests, or give plugin scripts a
documented way to read it.

---

## 3. `hermes -p NAME plugins enable <name>` picks the platform plugin and leaves the dashboard tab off

**Title:** `plugins enable <name>` enables only the platform plugin when a plugin has both a platform and a dashboard part

**What happens.** A plugin ships two parts under one name: a messaging platform (`platforms/<name>`)
and a dashboard tab. `hermes -p NAME plugins enable <name>` resolves the name to the platform
plugin and enables only that. The dashboard tab stays off, with no message saying so.

**Steps to reproduce.**
1. Install a plugin that has both `platforms/<name>` and a dashboard entry.
2. Run `hermes -p NAME plugins enable <name>`.
3. Run `hermes -p NAME plugins list`: the platform part is enabled, the tab is not. Open the
   dashboard: no tab.

**What we expected.** Either both parts are enabled, or the command says there are two and asks
which (or accepts something like `platforms/<name>` and `dashboard/<name>`).

**What we do instead.** Emporia's installer writes the dashboard plugin's entry into the
profile's config itself (P225), so it does not depend on the command.

**Suggested change.** Make `plugins enable` handle a name that matches more than one plugin: list
the matches, enable all of one plugin's parts, or ask. At the least, print what was and was not
enabled.

---

## 4. No way for a platform to mark its sessions as background

**Title:** Allow a platform to create its sessions hidden from the session list (for example `sessions_hidden`)

**What happens.** A platform whose conversations are agent to agent, not with a person, creates a
session for each conversation. They fill the session list, and the person has to hide them by
hand, again and again.

**Steps to reproduce.**
1. Run two agents on a platform plugin that opens a session per conversation.
2. Let them talk for a while.
3. Open the session list: it is mostly their conversations, mixed in with the person's own.

**What we expected.** A platform can say its sessions are background, so the list shows them only
on request.

**What we do instead.** Nothing automatic. The person hides the sessions by hand.

**Suggested change.** Add an optional field on the platform's entry (`PlatformEntry`), for
example `sessions_hidden`, applied when the gateway creates a session for that platform. The
sessions stay reachable (a "show hidden" toggle, or by id).
