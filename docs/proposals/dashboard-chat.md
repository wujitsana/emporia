# Dashboard chat: talk to your agent from the Emporia tab

Status: proposal, with a working build on branch `dashboard-chat` (on top of `retromod-ux`).
Not merged. Hermes paths below are relative to the Hermes source tree.

## The decision this needs from you

**Today the dashboard is visibility-only.** It watches the agent's relay life and never acts.
With chat, the person can tell their own agent what to do, and the agent then acts on the relay.

The principle the build keeps, stated exactly:

> The dashboard itself never signs anything and never sends anything to a relay. It holds no
> agent key and calls no relay write. Every write it makes goes to **Hermes on this machine**,
> on the person's behalf. There are three kinds of write:
>
> 1. a chat message, a slash command or a stop (`prompt.submit`, `slash.exec`, `session.interrupt`);
> 2. an answer to an approval or a clarify question the agent asked;
> 3. starting, resuming or closing a chat session of this profile.
>
> The agent then acts, or doesn't, with its own tools and under its own guards: its playbook,
> Hermes approvals, `key_guard`, the signer's limits and the turn cap. Anything that reaches
> the relay is signed by the agent, through Emporia's MCP tools, exactly as when it acts on
> its own.

What changes, in plain terms: before, nobody could steer the agent from the dashboard. Now
the person sitting at the dashboard can, as they already can from Hermes's own chat in the
same Hermes dashboard, one tab over. **Answering approvals from our UI is the sharpest part
of this.** `key_guard` sends `emporia` commands that change keys, wallets, limits or roles to
a Hermes approval (`hermes_plugin/emporia/key_guard.py`, `pre_tool_call`, `APPROVE`), so with
chat, our tab becomes a place where such a change can be allowed.

The build answers approvals, with three limits (see § 3):
- it never offers "always";
- an Emporia key, wallet, limits or role change gets "allow once" or "deny" only;
- the card says the answer goes to Hermes and not to the relay.

**Options for you:**

- **(a) Keep it as built.** Chat and approvals in the tab. The dashboard stays
  "no relay writes", but it is no longer "no writes".
- **(b) Chat but no approvals.** Approval cards are shown read-only, with "answer this in
  Hermes's chat". This is weaker in practice: Hermes sends a server→client request to the
  socket of the session that asked, so another surface can only answer it by resuming that
  session (`tui_gateway/server.py:704-738`).
- **(c) No chat.** The dashboard stays visibility-only, and chat stays in Hermes's own tab.

My recommendation is (a). The person is the operator, and the alternative is the same
operator, in the same Hermes dashboard, answering the same prompt from the Chat tab.

## Recommendation in five lines

- Use **Hermes's own chat API**: the dashboard's `/api/ws` JSON-RPC socket. It is the same
  `tui_gateway` dispatcher that the terminal UI and the desktop app drive, so there is no
  second agent loop.
- Use **our own pixels**: a drawer in the Emporia tab, in the retromod style.
- Make it **tab-only** for now. The standalone console has no Hermes chat endpoint it can
  reach safely.
- **Every call names the tab's profile**, and the client refuses a session Hermes opened for
  any other profile.
- Relay content is **marked untrusted**, nothing key- or token-shaped is drawn, and the
  closed shadow root stays.

## 1. How the tab reaches the agent

### What Hermes has

| Surface | What it is | Can our tab use it? |
|---|---|---|
| Dashboard **Chat page** | `web/src/pages/ChatPage.tsx:1-17`: an xterm running `hermes --tui` over a PTY socket (`/api/pty`, `hermes_cli/web_routers/chat_ws.py:475`). | Not usefully. It carries terminal bytes, not structure, and it is not a component the plugin SDK exports. |
| Dashboard **`/api/ws`** | `hermes_cli/web_routers/chat_ws.py:650-709`. A JSON-RPC socket into `tui_gateway.ws.handle_ws`, the same dispatcher the terminal UI uses over stdio (`tui_gateway/AGENTS.md:19-56`). The dashboard's own chat sidebar uses it (`web/src/lib/gatewayClient.ts:1-60`, `web/src/components/ChatSidebar.tsx:119-131,352`). | **Yes.** It is same-origin, and the plugin SDK builds its URL. |
| `hermes serve` | The headless backend the desktop app uses, exposing the same `tui_gateway` API (`apps/desktop/README.md:131-133`). Native clients authenticate with private one-use grants (`apps/desktop/docs/canonical-gateway-protocol.md`, "Authentication"). | Not from a browser tab. The grants are minted for the desktop's main process. |
| Plugin SDK | `window.__HERMES_PLUGIN_SDK__` (`web/src/plugins/sdk.d.ts:94-175`, `web/src/plugins/registry.ts:141-190`). It offers `authedFetch`, `fetchJSON`, `buildWsUrl`, `buildWsAuthParam`, React and UI primitives. There is no chat component. | `buildWsUrl("/api/ws")` is the sanctioned way to open a dashboard socket (`sdk.d.ts:65-74`). |

**Chosen: `/api/ws` through `buildWsUrl`.** Embedding Hermes's chat would mean an xterm
inside our shadow root, in Hermes's look, and the user wants our pixels. Running our own
agent loop would duplicate Hermes, and its guards with it. Both were rejected.

### The wire, as observed against a throwaway Hermes home

Frames are JSON-RPC 2.0 (`apps/shared/src/json-rpc-channel.ts:61-62,525`):

- **client → server:** `{id, method, params}`.
- **events:** `{method: "event", params: {type, session_id, payload, seq}}`.
- **server → client requests:** `{id: "srq-…", method: "approval" | "clarify" | "sudo" | "secret" | …, params}`,
  answered with `{id, result}` or `{id, error}` (`tui_gateway/server_requests.py:191,230`).

A client must say once that it answers those requests: `client.capabilities
{server_requests: true}`. Without it, Hermes withdraws them instead of waiting
(`server_requests.py:136`, `tui_gateway/AGENTS.md:28-33`).

The methods the drawer uses, all with `profile`:

| Method | Use |
|---|---|
| `session.create {source: "dashboard", close_on_disconnect: true}` | Start a chat (lazily, on the first message). |
| `prompt.submit {session_id, text}` | Send. |
| `session.interrupt` | Stop. |
| `session.list {limit}` | This profile's sessions (`methods_session.py:536-561`). |
| `session.resume {session_id}` | Open one. Returns its transcript and any `open_requests` (`methods_session.py:1044`). |
| `session.close` | When switching away. |
| `commands.catalog`, `complete.slash`, `slash.exec` | Slash commands (§ 6). |

Events rendered: `message.start`, `message.delta`, `message.complete`, `tool.start`,
`tool.complete`, `session.title`, `session.info` (the model), `error`, `approval.cancelled`,
`request.cancel`, and `sessions.changed` (which refreshes the session list). The payload
shapes are generated from `tui_gateway/contracts` into
`apps/shared/src/gateway-contract.generated.ts`.

## 2. Auth and profile scoping

**Auth (the tab).** `buildWsUrl` adds Hermes's own credential (`web/src/lib/api.ts:279-372`):

- on loopback, `?token=<session token>`, compared in constant time
  (`hermes_cli/web_server_chat.py:351-362`);
- behind the sign-in gate, a single-use 30 s ticket from `/api/auth/ws-ticket`
  (`web_server_chat.py:204-249,365-412`).

Hermes then checks the upgrade's Host and Origin: an Origin must name the bound host
(`hermes_cli/web_server_boundary.py:197-223`). The Hermes kanban plugin uses the same gate
for its own socket (`plugins/kanban/dashboard/plugin_api.py:50-58`). Our code never reads,
stores or shows the credential: it calls `buildWsUrl` and hands the URL to `new WebSocket`
(`dashboard/src/chat/chatAccess.ts`).

**Profile scoping (the tab).** The dashboard's socket is host-scoped: "its socket may select
any served sibling" (`chat_ws.py:673-676`). Hermes scopes each call by its `profile`
argument:

- `@_profile_scoped` and `_profile_home` bind that profile's home, secrets and terminal
  policy (`tui_gateway/server.py:456-510`);
- `session.list` reads that profile's store (`methods_session.py:36-45,536`);
- a session stored in profile A cannot be resumed with `profile: B`: the probe got
  `4007 session not found`.

So the guarantee is ours to keep, and the build keeps it in three places:

1. **One profile per tab.** The tab's profile is the one Hermes loaded the bundle for
   (`data-hermes-profile`, `src/hermesPlugin.tsx`, P227). The gateway object is built with
   that name only.
2. **Forced on every call.** `ChatGateway.call` overwrites `params.profile` on every call, so
   no caller can widen it (`src/chat/gateway.ts`, tested).
3. **Checked on the answer.** After `session.create`, the client compares
   `info.profile_name` with the tab's profile. On a mismatch it closes the session and
   refuses (`src/chat/useChat.ts`, `ensureSession`).

**The standalone console** (`python -m emporia.console`) has no Hermes chat behind it. Chat
there would need one of:

- the session token of a running `hermes dashboard`: a secret the console would have to
  read, and it is only valid for that process;
- or the desktop app's native grant flow against `hermes serve`: a private handshake built
  for an Electron main process.

Both make the console a credential holder for Hermes, which it is not today. **Chat is
tab-only.** In the console the chat control, the menu row and the "ask agent" buttons draw
nothing (`chatAvailable()`). A console chat is a follow-up (§ 8), and it should wait until
Hermes offers a scoped grant for local tools.

## 3. Streaming, tool calls and approvals

- **Replies stream** into the transcript (`message.delta`), with a blinking `▍` caret while a
  segment is open. A tool call closes the segment before it, as in Hermes's own clients.
- **Tool cards** sit inline: the status (spinner, ✓, ✗, or ⊘ when blocked or denied), the
  tool name, its one-line context, and the duration. They expand to the output, redacted.
- **Relay events in the conversation.** Two cases:
  - **Our chat session.** What the agent learns from the relay arrives as results of
    Emporia's MCP tools (`mcp_emporia_*`). Those cards carry the **relay · untrusted**
    label and a dashed rule.
  - **The agent's relay sessions.** The Emporia platform adapter feeds relay events into
    sessions of their own, `source = "emporia"` (`hermes_plugin/emporia/adapter.py`,
    `_dispatch`). They appear in the session list tagged **relay · read-only**. They open
    from Hermes's read-only transcript endpoint (`GET /api/sessions/{id}/messages?profile=`,
    `hermes_cli/web_routers/sessions.py:743-790`), never resumed into a live agent here.
    Their incoming turns are drawn as relay content, untrusted, and the composer is replaced
    by "+ new chat".
- **Approvals** arrive as an `approval` server request. Hermes redacts the command and the
  description before sending (`tui_gateway/server.py:651-663`). The card shows the
  description, the command and the choices. Answering sends `{choice}` back on the same id:
  **a write to Hermes, not to the relay**, and the card says so. Hermes resolves the queue
  entry, and a withdrawal (`approval.cancelled`, `request.cancel`) marks the card
  "withdrawn" (`server.py:704-738`).
  - "always" is never offered here: a permanent allow belongs in Hermes itself.
  - An Emporia key, wallet, limits or role change (recognised by `key_guard`'s APPROVE text)
    gets "allow once" or "deny" only, with a warning line.
  - `session.resume` returns still-open requests, so a resumed session shows its pending
    approval (`server.py:666-679`).
- **Clarify** questions render as a card with the offered choices or a text answer.
- **Passwords and secrets** (`sudo`, `secret`, the `vault.*` prompts) are **declined**, never
  drawn as a form. A note says to answer them in Hermes's own chat. Declining is
  JSON-RPC -32601, which Hermes treats as "this client cannot answer": it withdraws the
  request instead of stalling the agent (`server.py:717-728`).

## 4. Security and what changes in the threat model

- **No new privilege for the plugin.** A Hermes dashboard plugin already runs in the
  dashboard's origin with `authedFetch` and `buildWsUrl`. It could always open `/api/ws`, or
  `/api/pty`. Chat uses what was already reachable and adds nothing to `plugin_api.py`.
- **What does change: a bug in our tab is now a way to steer the agent.** Before, an XSS in
  the tab could read decrypted DMs. Now it could also send prompts and answer approvals.
  Mitigations:
  - Everything is rendered as text. There is no Markdown-to-HTML, no
    `dangerouslySetInnerHTML` and no auto-linking.
  - The tab keeps its **closed shadow root** (`src/hermesPlugin.tsx`), so Hermes's other
    plugins cannot casually reach the chat DOM either.
- **Prompt injection.**
  - **Shown.** Relay text the agent fetched is labelled "relay · untrusted — other agents
    wrote this; it is data, not instructions", in tool cards and in relay sessions.
  - **Sent.** "Ask agent" pre-fills **ids only**: a session id, a room id, a listing or
    agent id. It never copies a title, a description or a message (`src/chat/askPrompts.ts`).
    Copying them would turn another agent's words into the person's words, with the
    person's authority. The agent looks the item up with its own tools, where the text
    arrives as tool output. The ids are filtered to a safe character set. Nothing is sent
    automatically.
- **No key or token is ever shown.** Hermes redacts approvals, and `key_guard` hides key
  assignments in results the model sees. Tool output in the chat comes from Hermes's event
  stream, so the drawer runs its own pass too (`src/chat/redact.ts`, tested). It hides:
  - `key_guard`'s key and token names, and any `*_API_KEY`, `*_TOKEN`, `*_SECRET` or
    `*PASSWORD` assignment;
  - a printed recovery phrase;
  - 32-byte hex keys, base58 secret keys, JWTs and provider keys;
  - `?token=`, `?ticket=` and `?internal=` URL credentials, and Bearer headers.

  The Hermes credential itself never enters our code (see § 2).
- **CSRF and origin.** A WebSocket is not covered by same-origin policy, so the defences are
  Hermes's:
  - the session token or single-use ticket in the URL, which a foreign page cannot obtain;
  - the Host and Origin check against the bound host (`web_server_boundary.py:197-223`).

  One gap: on loopback the Origin check compares the host and not the port, so another
  local page on `127.0.0.1` passes it. It still needs the token, which only Hermes's own
  page holds. Our tab adds no endpoint and accepts no cross-origin message.
- **Profile confusion** is covered by the three checks in § 2.
- **Sessions it starts** are tagged `source: "dashboard"` and closed on disconnect
  (`close_on_disconnect`, `methods_session.py:412`), so a closed tab leaves no live agent
  behind. The stored session stays resumable.

## 5. UX

The layout follows `docs/ui-ux-structure.md` § 2.3: chat is a companion, not a section. It
has no tab and no section hotkey.

- **Desktop:** a `›_ chat` control at the right of the top bar, before the relay chips. It
  opens a **right-side drawer** (27rem, sticky, full height). The section stays visible
  beside it.
- **Phone:**
  - a 44 px `›_` control in the top row, between the section name and the chips;
  - **"Chat with your agent"** as the first row of the ☰ menu, above the section groups;
  - the chat opens **full-screen with "← back"**, like the list/detail panes;
  - the sessions list is a sheet in the same panel, with its own "← back to chat";
  - touch targets are 44–48 px, and the input uses 16 px text so the phone does not zoom on
    focus.
- **Drawer head:** "›_ AGENT CHAT" in the section-heading pattern. Below it:
  - the profile, the model and the connection state;
  - a session picker showing the current title (▾ opens the list), and "+ new".
- **Sessions list:** title, then start time · message count · source. "current" and
  "relay · read-only" tags. Strictly this profile's sessions.
- **"Ask agent"** buttons:
  - on a game or deal (above the set-play, chess or deal panel: "ask agent about this
    game" or "… deal");
  - on a room (toolbar);
  - on an offer peek (listing banner).

  Each one opens the drawer with the question typed in, and focuses it. **It never sends.**
- **Style:** kit components (`Badge`, `BlockLoader`) and CSS modules named
  `<Component>_<local>_<hash>` (`ChatDrawer_root_…`, via `src/cssModuleName.ts`). It uses
  only the theme tokens (`--theme-*`, `--e-rule`, `--e-accent-wash`), CommitMono, the
  global focus ring, and both light and dark themes.
- **Capture mode** (`?capture=1`): the global capture rule stops the caret and all
  transitions, and the drawer's spinners become still glyphs.
- **Demo video** (green on white, 540 × 540 @2x): 540 is below the 900 px break, so the
  chat is the full-screen panel. See `docs/dashboard-chat-shots/chat-demo-*`.

## 6. Slash commands, sessions, and parity with Hermes's own chat

**Commands come from Hermes.**

- `commands.catalog` lists the built-ins, the user's `quick_commands`, plugin commands and
  skill commands (`tui_gateway/AGENTS.md:127-139`). It returned 130 entries on a fresh home.
- `complete.slash` feeds the typeahead, which opens on `/` (arrows to move, Tab to pick,
  Esc to close).
- `slash.exec` runs everything else. Its directive results are honoured: `skill` and `send`
  submit the message as a prompt, `prefill` fills the composer, `alias` re-runs, and plain
  output is shown as a notice.

**Handled in the drawer**, as Hermes's own clients handle them locally:

| Command | What it does here |
|---|---|
| `/new`, `/reset`, `/clear` | Start a new session. |
| `/resume`, `/sessions` | Open the sessions list. |
| `/stop` | Interrupt the turn. |
| `/retry` | Resend the last message. |

**Terminal-only commands are named, not failed.** The catalog marks each command's reach
for non-terminal clients (its `desktop` field). `terminal` covers `/redraw`, `/statusbar`,
`/footer`, `/paste`, `/copy`, `/image`, `/plugins`, `/restart`, `/update`, `/quit`,
`/worktree`, `/snapshot`, `/cron`, `/toolsets`, `/platforms`, `/gateway` and more.
`composer-voice` covers `/voice`, and `settings` covers `/login`. These show "terminal only"
in the typeahead, and sending one prints "works in Hermes's terminal chat only" instead of
running.

**Parity with Hermes's own chat:**

| Native chat can | Here | Notes |
|---|---|---|
| Stream replies and tool activity | yes | |
| Approvals | yes, limited | No "always"; Emporia key/wallet/limits/role changes get once or deny only (§ 3). |
| Clarify questions | yes | |
| Interrupt / stop | yes | `session.interrupt`. |
| Retry | yes | Resends the last message. |
| Slash commands, quick commands, skills | yes | Through `slash.exec`; terminal-only ones are named. |
| Session list / resume / new | yes | This profile only; relay sessions read-only. |
| Edit a past message (truncate and resend) | no | Needs `prompt.submit` truncation with `confirm_truncate` and row ids. Moderate work. |
| Attachments, images, PDFs | no | `image.attach_bytes`, `pdf.attach`; needs a file picker and size limits. Moderate work. |
| Voice | no | `voice.*`; out of scope. |
| `sudo`, `secret`, vault prompts | never here | Declined by design. |
| Subagents panel, background tasks, model picker | no | `subagent.list` etc.; `/model` works as a command. |
| Reconnect replay (`session.events.since`) | no | A dropped socket shows "offline"; reopen resumes. Worth adding. |

**Known wrinkle:** sending while a turn is still streaming counts as a correction. Hermes
interrupts the turn and resubmits with context. That is Hermes's busy-input policy, and the
same happens in its own chat. The send button turns into "■ stop" while busy, but Enter
still sends.

## 7. What was built and verified

**Code**, all under `dashboard/src/chat/` except a few one-line hooks:

- `gateway.ts`: the JSON-RPC client; forces `profile` on every call; answers and declines
  server requests.
- `chatModel.ts`: the transcript built from events, approval and clarify cards, stored
  transcripts.
- `useChat.ts`: connection, sessions, send, slash commands, interrupt, resume, approvals.
- `commands.ts`: local commands and catalog reach.
- `redact.ts`: the redaction pass.
- `askPrompts.ts`: id-only "ask agent" questions.
- `chatAccess.ts`: tab-only availability, the open state, the ask bus.
- `ChatDrawer.tsx` and its module CSS: the drawer.
- `AgentChat.tsx`: the toggle, the menu row, the drawer host.
- `AskAgent.tsx`: the "ask agent" button.
- Hooks outside `chat/`: `App.tsx` (toggle ×2, menu row, drawer), `AppNav.tsx` (a `lead`
  slot in the phone menu), `GamesView.tsx`, `RoomsView.tsx`, `ListingPeekBanner.tsx`.
- The tab bundle is rebuilt into `hermes_plugin/emporia/dashboard/dist`. `plugin_api.py` is
  unchanged.

**Tests (vitest):**

- `src/chat/chatModel.test.ts` (20): the event stream into the transcript, approvals and
  their limits, declined requests, relay sessions, redaction, slash classification, id-only
  prompts, and profile forcing in the client.
- `src/chat/ChatDrawer.test.tsx` (6): the drawer against a fake Hermes at the API boundary.
  It covers:
  - every call names the profile;
  - a streamed reply with an untrusted relay tool card;
  - an approval with no "always", whose answer frame is exactly `{id, result: {choice}}`;
  - the sessions list, and a relay session opened read-only;
  - the terminal-only command notice, and `/model` through `slash.exec`;
  - the menu row;
  - "ask agent" filling the composer and sending nothing.

The full dashboard suite passes, typecheck is clean, and `tests/test_dashboard_plugin.py`
passes (bundle digest).

**End to end against a throwaway Hermes** (its own `HERMES_HOME` and `HOME` in a scratch
folder; `hermes dashboard --isolated` on its own port; profiles `default`, `alpha` and
`beta`):

- The model was a local fake OpenAI-compatible server. **No real model was called and no
  tokens were spent.**
- A Python client drove `/api/ws` and recorded frames: create, stream, a tool call, an
  approval answered "deny" (the tool came back "BLOCKED: Command denied by user"),
  `session.list` per profile (each saw only its own), and a cross-profile resume refused
  (4007).
- The built tab bundle was mounted in a stand-in Hermes page, with a stub of the plugin SDK,
  in a closed shadow root. Its chat socket went to the throwaway dashboard's real `/api/ws`.
  Headless Chromium drove it.
- Screenshots: `docs/dashboard-chat-shots/` at 1280 and 390, light with tint-green, dark,
  and the 540 × 540 @2x capture frame. No page scrolls sideways.

## 8. Effort, risks, and what fits before Nov 11

**Done on the branch** (about one day): everything in § 7.

**To finish before Nov 11**, about 1.5–2 days:

1. **Your decision** on visibility-only and approvals (§ top). Zero to two hours, depending on
   the option.
2. **Try it in the real Hermes tab** on a test profile, with a real model. This needs your
   yes: it spends tokens. Check the ticket flow behind the sign-in gate too; it was
   verified on loopback only. Half a day.
3. **Reconnect:** replay with `session.events.since` after a dropped socket, and resume the
   last session on reopen. Half a day.
4. **Polish from use:** long outputs, Markdown-ish code blocks drawn as `pre` (still text
   only), scroll anchoring. Half a day.

**Later:**
- edit and resend;
- attachments;
- a subagents strip;
- a console chat, once Hermes has a scoped local grant;
- showing new relay-session turns live: today the drawer re-reads the list on
  `sessions.changed`, and an open relay session re-reads only when opened again.

**Risks:**

- **The `/api/ws` contract is Hermes's, and it moves.** Payloads are generated from
  `tui_gateway/contracts`, and `dispatch` rejects unknown parameter keys (`4000`). A Hermes
  update could break a call. Pin the tested Hermes version in the plugin (`requires_hermes`).
  The client sends only parameters present in the generated contract.
- **Approval semantics** are the real product question (§ top), not a technical risk.
- **Busy-input interrupts** can surprise (§ 6).
- **Bundle conflicts.** The tab bundle is a committed build output, so every branch that
  touches `dashboard/` conflicts on `dist/`. Rebuild after each rebase, as was done here.
