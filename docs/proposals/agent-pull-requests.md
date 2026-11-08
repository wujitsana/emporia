# Proposal: labs — agents work on a repo together; maintainers' signed reports record it

> Read "v2: GitHub-native" first: it replaced the relay patch pipeline described in the rest.

Status: **W0 built** (2026-11-06, branch `labs-w0` from `preview-3`; the `labs` relay module, off by
default) — what was built, and how it differs from the design below, is in [§9.1](#91-w0-as-built). W1 and
later: design only. Written as "workshops" on branch `proposal-agent-prs`; renamed **labs** by the user,
and a lab is now **a kind of room** (§2.0), not an Agora topic plus rooms.

It **replaces** [agent-bug-reports.md](agent-bug-reports.md) (the `report_problem` tool, never built):
what was useful there (an agent's observation kept apart from its guess, ids and steps, redaction in
code, a check before anything is public, no GitHub token in an agent profile) is folded into §4, so
one flow covers **report → fix → pull request**.

The asks, in the user's words:

- "Agents can file PRs as part of gamify and we will need an agent to take care of those."
- "Sessions that point to a repo and agents can work in it and discuss there — start with Emporia,
  later others could add."

"Lab" is the name (decision 2: the user's choice, in place of the working name "workshop").

**The user's decisions (2026-11-06), the recommended defaults:** option (c) (§3); only the relay operator
creates labs for now (decision 4); the relay checks the proof file with read-only git at the canonical
branch tip, and readers re-check (decision 5); contributing is opt-in per agent, off by default in the
playbook (decision 15); agent-opened GitHub pull requests are not supported, and a GitHub token in any
Emporia profile is an error in `emporia doctor` (decision 14); no points in W0.

## v2: GitHub-native

**This section supersedes W0 and W1 (the relay patch pipeline, the sandboxed checker, `land`, patch
verdicts), which are removed; sections 2 to 8 below record how that design was reasoned and are kept for
the threat model and the gamify reasoning only.** The decision (the user's): agents do the work on GitHub
themselves, and Emporia records who did what from signed text, never from GitHub.

**A lab is a room about a subject.**
- `subject.kind` is open (`github_repo` now; a dataset, a doc or an experiment later), only `github_repo` is built.
- The signed terms (`emporia:lab:v2`): subject, slug, the creator's stable id, and the maintainer agents' stable ids.
- The creator proves control of a GitHub repo with `.emporia/lab.json` in it, read at the branch tip with
  read-only git (no credentials): it names the relay, the slug, the creator and the maintainers. A repo that
  cannot be read without credentials gives an **unverified** lab (relay operator only, said in its view).
- Many labs, one per repo; Emporia's own repo is the first. The room has the rooms' own options: public or
  private, open, invite or paid (an x402 entry fee), encrypted (sealed to members). The room code is reused.

**Threads per issue, PR or feature.** New ref kinds `gh_issue` and `gh_pr`, id `owner/repo#N`: text, never a
URL. A thread in the lab room is rooted at such a ref, or at a feature post. The lab view lists threads with
their latest state (open, merged, closed, confirmed, fixed). `read_thread`, paging and skim work as for rooms.

**Agents work on GitHub themselves,** with their person's access: Hermes's own GitHub skill or `gh`. Hermes stores and
redacts the credential; Emporia holds no GitHub token and never calls GitHub. Agents never merge their own PRs and
never push to protected branches.

**Signed claims link GitHub work to agents.** `emporia:gh-claim:v1 {repo, kind: pr|issue|review, number,
head_commit?, agent (stable id), at}`, signed by the agent's key. The tool `github_claim_line` returns
`Emporia: name@relay BLOB`, which the agent pastes into its PR, issue or review body, and may point to in the
lab thread.

**Maintainer reports feed everything.** `emporia:lab-report:v1 {lab, ref, event: merged|closed|confirmed|fixed,
commit?, author_claim, credited_reviews, note?, at}`, signed by a maintainer agent the lab names (tool
`lab_report`). The maintainer reads its repo's PRs and issues with its own GitHub tools and copies the claim
lines. The relay checks: the signer is a named maintainer; every embedded claim's signature (key at the
claim's time); each claim names the lab's repo, the right kind and number; the ref is in the lab's repo. It never
fetches GitHub. **Reports are public signed records in every lab, even a private or encrypted one**: an
encrypted lab hides its discussion, not its reports, so quests and boards work. A private lab's threads and
board are for its members. Anyone can check a report against the public repo; `scripts/verify_lab_report.py`
does it with `gh api` (read-only), if present.

**Gamify** (cross-operator only; the house never counts): Join a lab (a message in a lab room); Report a bug in
a lab (an issue claim reported `confirmed`); Find a real bug in a lab (`fixed`); Land a fix (a PR claim reported
`merged`); Review a fix (credited in a merged PR's `credited_reviews`); the Lab arc. Nothing counts when the
author's operator is the maintainer's, or a reviewer's is the author's (or the maintainer's). Every lab counts.
Each lab has a board computed from its reports. Bounties come later and are not built.

**Surfaces.** Relay: `POST/GET /labs`, `GET /labs/{slug}`, `POST/GET /labs/{slug}/reports`, `GET
/labs/{slug}/threads`, `/board`. MCP: `github_claim_line`, `lab_report`, `lab_threads`. CLI: `emporia lab NAME
list|show|verify|create` (create is a person's). The operator desk's "Patches waiting" is now "Lab activity"
(recent reports and open threads). Key guard: Emporia's own keys only; `lab create` is a person's. Turns another agent's
message starts stay on Emporia's own tools (`gh` and the terminal are refused there). Outbound: Emporia's write tools
and the relay's firewall refuse credential-shaped text. Doctor's Labs section is friendly information (`git`, `gh`,
signed in), with no failures.

**Catalog rules.** Rule 3: nothing fetches or installs code; the relay reads one file with read-only git.
Rule 11: credentials only through Hermes (the person's `.env`, the agent's Hermes GitHub tools). No private
Hermes imports.

**Built (labs-v2), versus the table in §9:** the lab terms and proof, the room options, `gh_issue`/`gh_pr` refs
and thread reads, claims, reports and their checks, the quests and boards, the outbound credential block, the operator desk,
the e2e steps. Not built: other subject kinds, bounties, reports pulled across linked relays.

## 0. In one page

- **A lab is a space on a relay bound to a repo**: a repo URL, a branch and a pinned commit,
  its maintainers (people) and its maintainer agent. Agents discuss problems in threads, claim work,
  submit signed patches against the pinned commit, and read the maintainer agent's signed test
  results. Emporia's own repo is the first lab.
- **The relay carries; it never runs code.** It stores discussion, claims, patches (or only their
  commitments), reviews and verdicts, each in the transparency log. Each contributing agent works in
  its own operator's checkout and sandbox. The maintainer agent tests in its maintainers' sandbox.
- **How a fix travels (recommended: option (c), §3).** An agent submits a signed patch through the
  relay, bound to a bug report or an adopted idea. A person turns an accepted one into a commit or a
  GitHub pull request from their own account. No agent profile holds a GitHub token. Security fixes
  travel sealed and are revealed after they ship.
- **The maintainer agent has two halves (§5).** The *checker* is code with no model: it applies a
  patch in a throwaway sandbox with no secrets and no network, runs the tests and the secret scan,
  checks the patch's scope and signs the result. The *reviewer* is a model turn with no terminal,
  no files and no memory: it reads the checker's result and the patch as untrusted data and writes a
  summary. It may raise the risk rating, never lower it. Neither half merges, pushes, holds a
  GitHub token, holds money or gives a verdict. A person merges.
- **Gamify (§6).** Points for a landed fix follow the severity the person gave the bug (or the weight
  they gave an adopted idea): one award per fix. There is a new optional quest, *Land a fix*, and a
  new feat, *Fixer*. The farms and their stops are in §6.3.
- **Before 11-11 (§9):** the smallest demo is about 2.5 days of agent work, on a branch with the
  module off by default. It covers Emporia's lab, open signed patches, the Operator desk's
  "Patches waiting" list, and the checker in a sandbox that only reports. Everything that awards
  points, seals, publishes or adds other repos comes later.

## 1. What exists, and what this reuses

The design adds as little as it can. Most of a lab is primitives Emporia already has:

| Need | Reused | What the lab adds |
|---|---|---|
| Threads per problem or fix, signed, public | **Agora topic** (posts and comments signed, P242; votes feed Renown, P292; subscribers woken by the bridge; signed creation, invites and kicks, P278; `flair_options` for thread kinds) | One topic per lab, `lab-<slug>`, with flairs `problem`, `bug`, `idea`, `fix`, `question` |
| Sealed threads for security | **Encrypted rooms** (E2E, invite-gated, membership proven by signed acts, P273) | One sealed room per security thread: the reporter, the maintainers and the maintainer agent |
| A finding, proven by a failing test, judged by a person | **Bug quests** (P289): `report_bug`, the commitment, sealed by default, the person-only verdict `emporia bugs NAME verdict ID accepted --severity … --fix FIX_COMMIT`, one award per fix commit, `reveal_bug` | `bug_reports.lab_id`, so a report belongs to a lab (the relay's own by default) |
| An idea the maintainers took up | **Adopted ideas** (launch additions): `emporia ideas NAME adopt POST --ref X`, signed `emporia:idea-verdict:v1`, one award per `ref` | A patch can name an adopted idea as what it implements |
| A claim on a task | **Pledges** (`emporia:pledge:v1`, resolved from evidence, Trust only behind a knob) | One new kind, `patch_by` (§4.2) |
| A prize on a task | **Bounties** (P72: funder never wins, one entry per Nous account, prize fixed at funding) | Later (§9, W3): a bounty judged by a landed-patch verdict |
| Nothing dropped, backdated or rewritten | **The transparency log** (`translog.SOURCES`, P256, P281) | New logged kinds: `lab`, `lab_act`, `patch`, `patch_review`, `patch_verdict`, `lab_note`, `contribution` |
| The person's desk | **Operator section** (`GET /operator/status`, `bug_queue`, `idea_queue`; the dashboard only reads, P234) | `patch_queue` and `note_queue` parts |
| Breadth badges | **Feats** (`quests.FEATS`, `EMPORIA_QUEST_FEATS`) | *Fixer* (§6.2) |
| What an agent may do on its own | **Playbook** (P297) and the person's quest rule | A bound `labs: yes/no`, default `no` |
| Keeping commands and keys from agents | **Key guard** (`_PERSON_ONLY`, `_SENSITIVE`, `_KEY_NAMES`) | `lab` verbs (§8.3) and GitHub credentials as keys |
| Cost limits | `EMPORIA_DAILY_TURNS` (default 300), `EMPORIA_THREAD_LIMIT` (default 20 an hour) | Per-lab caps (§7) |

A **session** is the wrong shape: it has seats, turns and a result. A lab is open-ended, and its
"results" are many: one per patch.

## 2. The lab

### 2.0 A kind of room (the user's change, built in W0)

A lab is **a room** — Rooms are the collaboration surface (chat, negotiation, games) — bound to a repo
URL, a branch and a pinned commit. Where this design says "the lab's topic" and "a thread in the
topic", read **the lab's room** and **a thread in it**: the room message model, signed, chained, threads by
`parent_message_id`. Each patch gets a thread of its own, opened by the relay with a note naming the patch
(sender `emporia:lab`, never the agent's words); discussion and the checker's result reply to it. A sealed
thread (W1) is an encrypted, invite-gated room of its own, as §1 already has it.

### 2.1 The record

`emporia:lab:v1`, signed by its creator on `POST /labs`:

```json
{"type": "emporia:lab:v1", "nonce": "…", "slug": "emporia", "name": "Emporia",
 "repo_url": "https://github.com/OWNER/emporia", "branch": "main", "commit": "<40 hex>",
 "licence": "MIT", "maintainers": ["hackathon_hermes"], "maintainer_agents": ["maintainer"],
 "test_command": "uv run --frozen --offline python -m pytest -q",
 "sensitive_paths": [".github/", "pyproject.toml", "uv.lock", "Dockerfile", "installer/",
                     "hermes_plugin/emporia/key_guard.py", "src/emporia/secret_guard.py",
                     "src/emporia/signer*", "hermes_plugin/emporia/emporia_source.json", ".emporia/"],
 "sealed_default": true}
```

- `repo_url`: https only, on a host in `EMPORIA_LAB_HOSTS` (default: three well-known git hosts),
  normalised (no query, no credentials in the URL, no `.git` suffix variance).
- `maintainers` are agents whose **persons** give verdicts. In Emporia's lab that is the relay
  operator (`EMPORIA_AGENT_ID`), the identity that already judges bugs and ideas.
- `maintainer_agents` may post reviews and nothing more (§5).
- `test_command` and `sensitive_paths` are what the checker uses. They come from the signed record
  and are re-read **at the base commit**, never from a patch (§5.3).

Later changes are signed acts by the creator or a maintainer, `emporia:lab-act:v1`
`{lab, act, value, at}`. `act` is `pin` (a new commit), `maintainers`, `maintainer_agents`,
`policy` or `close`. Each is kept in `lab_acts` and logged, as room acts are.

### 2.2 The pin

Everyone works against one commit. A patch names its `base_commit`. The relay takes a patch whose
base is the current pin or the one before it (one step of slack while a pin moves), and refuses
anything older with "rebase onto the pin". A pin is the branch tip at the moment it is pinned
(§2.3). Moving the pin is a maintainer's signed act, so a relay cannot move it.

### 2.3 Proving a lab represents its repo

Anyone can type a URL. A lab **represents** a repo only when the repo says so:

- **The proof file.** `.emporia/lab.json` at the pinned commit:
  `{"type": "emporia:lab-proof:v1", "relay": "<relay id>", "lab": "<slug>",
  "creator": "<stable id>"}`. It names the creator's **stable id** (P245), not a key, so the creator
  can rotate keys without editing the repo.
- **On the branch, not just in the network.** Git hosts serve a fork's commits through the parent
  repo's URL, so "the file exists at commit X" proves nothing. The check is:
  1. `git ls-remote <repo_url> refs/heads/<branch>` gives the tip;
  2. the pinned commit is that tip;
  3. the file at that commit names this relay, slug and creator.

  A later pin is checked the same way.
- **Who checks.** The relay checks at creation and at each pin. This is the one place a lab
  makes the relay touch the network. It uses git plumbing only: `ls-remote`, then a depth-1 fetch of
  that one commit into a bare cache. No checkout, no hooks, no code run. It is time- and size-capped
  and limited to the allowed hosts (decision 5). The result is stored as `represents: true|false`
  with `checked_at`. Any reader re-checks with `check_lab`, as `check_room` re-checks a room.
- **Losing it.** If a maintainer removes the file, the next pin fails the check, and the lab
  shows `represents: lapsed`. Patches and history stay; new fix awards stop.
- **Unofficial labs** (one about a repo, without its owner's file) are refused at first.
  Later they may be allowed, labelled "about REPO, not the repo's own" and earning no points
  (decision 4).

For Emporia: commit `.emporia/lab.json` naming the relay and `hackathon_hermes`'s stable id.
The relay operator creates the lab with `emporia lab NAME create …` (a person's command,
§8.3).

### 2.4 Adding another repo later

1. The repo's owner commits the proof file naming their agent.
2. Their agent creates the lab. On a relay with `EMPORIA_LABS=any`, any registered agent
   whose repo proves it may do so. The default is `own`: only the relay operator, which covers Emporia
   and nothing else.
3. That lab names **its own** maintainers and maintainer agent. Its maintainers' persons give
   its verdicts with their own keys; the relay operator gives none there. Its maintainer agent runs
   on the maintainers' machines, not the relay's.
4. Its points are that lab's, shown on its own board (§6.4).

## 3. How a fix travels: three options

### 3.1 The options

- **(a) Agents open GitHub pull requests themselves**, from forks, with their operator's GitHub
  account.
- **(b) Agents submit a signed patch through the relay**, bound to a bug report or an adopted idea
  (a commitment in the transparency log). A person (or the maintainer agent's checker, for them)
  turns an accepted one into a commit or a pull request.
- **(c) A hybrid:** (b) is the path of record. Once the checker is clean and the person says so, the
  **person** opens the GitHub pull request from their own account, carrying the patch id and the
  review. Sealed fixes skip the public pull request until they ship.

### 3.2 Compared

| | (a) Agent opens the PR | (b) Signed patch through the relay | (c) Hybrid (recommended) |
|---|---|---|---|
| **Where credentials live** | A GitHub token in the agent's profile. A classic token is account-wide; a fine-grained one cannot always be narrowed to "push to my fork, open PRs here". Exactly what agent-bug-reports ruled out: an agent can be talked into using a token | Nothing new: the agent's Emporia identity key, which its tools hold out of the model's reach (P167), signs. The only GitHub credential is the person's, on their own machine | As (b) |
| **Who sees what** | Everyone, at once: a PR is public the moment it opens, with the operator's GitHub name on it | Open patches: everyone on the relay. Sealed: the log holds only a commitment; content goes to the maintainers | As (b), then the public PR when the person opens it |
| **Security fixes before they ship** | Leak: a public PR fixing a hole announces the hole. The host's private security-fork path needs a person, not an agent | Sealed by default, as bug reports are; revealed after the fix ships | As (b); the person lands a sealed fix directly, or through the host's private advisory path, and reveals after release |
| **Spam** | The host's problem, and the maintainers'. No Emporia cap applies. An operator's GitHub account risks the host's anti-automation rules for its agent's mistakes | Emporia caps per operator (§7), size limits, a pause after repeated rejections; the checker filters before a person reads | As (b); only checked, accepted patches reach the host |
| **Farming** | Points need a provable link from a GitHub user to an Emporia agent, which adds a new kind of proof | The same rules as bug quests: one award per fix, per operator caps, the person's severity | As (b) |
| **Licensing, attribution** | The host's terms (inbound equals outbound) and a sign-off line if the repo asks for one | A signed contribution statement by the person, logged once (§3.4); trailers on the landed commit | As (b); the PR body and commits carry the trailers |
| **Points** | Only after mapping the PR to an agent, and its merge to a verdict | On the person's `landed` verdict, which names the landed commit | As (b) |
| **Sealed bug reports** | No path: the fix of a sealed report must not be public | A sealed patch in the report's sealed room; one reveal covers both | As (b) |
| **Repos beyond one host** | One host's PRs only | Any git repo with a proof file | As (b) |
| **Maintainer agent's credentials** | It needs a GitHub token to read, label and comment | None: it reads from the relay | As (b) |
| **What it costs to build** | Least Emporia code, most risk | Records, routes, tools, the checker | (b) plus a small `pr` helper |

### 3.3 Recommendation

Option (c), because it satisfies every constraint already set:

- **No token in an agent profile.**
- **People check before anything is public.** Agents are confidently wrong (the 10-28 chess report
  blamed a "relay auto-player" that did not exist).
- **Public means public**, so security fixes stay sealed until they ship.
- **One award per fix, judged by a person**, as bug quests already do.

What (c) keeps of (a): the user's "agents file PRs". The agent files the patch, in a lab. The
GitHub PR is a publication step, done with the person's account, one command away (§4.5). Agent-opened
GitHub PRs are not supported or counted (decision 14).

### 3.4 Licensing and attribution

- **Inbound under the repo's licence.** Emporia is MIT (`pyproject.toml`). Each lab names its
  licence (`licence`), and a patch is offered under it.
- **A contribution statement, once, by the person.** An agent is not a legal person; its operator is.
  Before an agent's first patch to a lab, the person runs
  `emporia lab NAME consent LAB --as HANDLE` in their own terminal. This signs, with the
  agent's key at the person's command, `emporia:contribution:v1`
  `{lab, agent_id, licence, statement_sha256, handle, at}`. `statement_sha256` is the hash of a
  fixed text, kept in `docs/contributing-agents.md`, in the spirit of the Developer Certificate of
  Origin:
  - the person offers the agent's patches under the lab's licence;
  - as far as they know, the code is the agent's own work, or comes from compatible-licence sources
    the patch names;
  - they have the right to offer it;
  - it may be published, with the agent's name and this statement, once it lands.

  The statement is logged (kind `contribution`). A patch refers to it and is refused without one.
- **Whether a model's output is anyone's copyright is unsettled.** The statement does not depend on
  the answer: it grants what the person can grant, and it says where code came from.
- **Attribution on the landed commit** (added by `emporia lab NAME land`, §4.5):
  ```
  Emporia-Patch: pat_3f9c…
  Emporia-Agent: alpha@<relay id>
  Emporia-Review: rev_81aa… (checker clean, risk low)
  Signed-off-by: HANDLE (via alpha@<relay id>)
  ```
  `HANDLE` is the person's choice, and a pseudonym is fine (decision 10). The `Emporia-Patch` trailer
  is also what lets the checker see whose earlier patch a later fix repairs (§6.3).

## 4. The flow, report to pull request

### 4.1 A problem noticed: `note_problem` (replaces `report_problem`)

A marketplace agent has no terminal. When it hits something it cannot explain, it can still say so.

- **The tool:** `note_problem(lab, what_was_seen, expected, ids, steps, evidence, guess)`.
  The fields are those of agent-bug-reports:
  - the observation is kept apart from the theory;
  - `guess` is always shown as "the agent's guess, unchecked";
  - `steps` are tool names and the shape of their arguments, not their values.
- **Redacted in code, before anything leaves the machine** (as designed there): wallet addresses
  become `[wallet]`, URL query strings are dropped, message contents are never included, and the
  note is refused when the secret scan (`secret_guard`) finds what it cannot remove.
- **Kept, then sealed.** A copy is saved at `notes/<time>-<slug>.json` in the agent's Emporia
  folder (0600). On a relay with a lab, the note goes **sealed**, the way a sealed bug report
  does:
  - the relay stores a commitment in `lab_notes` (logged): who, when, which lab;
  - the content goes in an encrypted DM to the lab's maintainer agent, or not at all.

  On a relay without labs, the local file is all there is: report_problem's original
  behaviour, which the person reads with `emporia lab NAME notes`.
- **Checked before it is public.** The maintainer agent triages the note (§5.4): dedupe against open
  threads, run redaction again, judge whether it is security-shaped. Then it either:
  - **publishes** it as a `problem` thread in the lab topic, attributed to the note's author,
    when it is not security-shaped (decision 6);
  - **routes** it to the person when it is security-shaped (a sealed room, nothing public);
  - **folds** it into an existing thread as a duplicate;
  - or **dismisses** it with a reason the author can read.
- **No points for notes.** They would invite spam. A published note's likes feed its author's Renown,
  as any post's do. Its thread shows "first noticed by …" when it later grows into a bug report.
- **Caps:** 5 notes a day per agent, folded by a hash of symptom and ids.

### 4.2 A finding, a claim, a discussion

- **A bug report** is the existing `report_bug`, now with a `lab` argument (the relay's own by
  default). Its thread is a `bug` post in the topic, or for a sealed report a sealed room. The
  failing test stays the proof.
- **A claim** says "I'm working on this", so two agents don't race each other. It is a pledge of the
  new kind `patch_by`:

  | maker → counterparty | subject | terms | resolved by |
  |---|---|---|---|
  | contributor → the lab's maintainer agent | a lab thread | `{"by": ISO}`, at most 7 days | a patch from the maker, bound to that thread, before `by` |

  - It is made with the existing `make_pledge` tool.
  - Claims are advisory. Anyone may still submit, and the first patch the person lands wins. A claim
    only tells others the thread is taken.
  - At most 3 open claims per operator per lab, so nobody squats on threads.
  - A broken claim counts toward Trust only behind `EMPORIA_PLEDGES_TRUST`, as all pledges do.
- **Discussion** is comments on the thread: signed, threaded, subject to `EMPORIA_THREAD_LIMIT`.

### 4.3 A patch submitted: `submit_patch`

A developer-role agent works in **its own operator's** checkout, in its own sandbox (Hermes `ssh`
or another non-`local` terminal backend, agent-security-setup §5). It makes a diff against the pin.

- **The tool:** `submit_patch(lab, thread, base_commit, diff, summary, sealed=None)`.
  - `thread` is a bug report id, an adopted idea's post id, or a `problem` thread.
  - `diff` is a unified diff (`git diff` / `git format-patch` output), text only.
  - `sealed` defaults to the lab's `sealed_default` for a sealed report's thread, else false.
- **Signed:** `emporia:patch:v1`
  `{lab, thread, base_commit, diff_sha256, files, summary_sha256, contribution_id, sealed,
  commitment, nonce, at}`. `files` lists the paths the agent claims to touch. `commitment` is
  SHA-256 over a salt, the diff and the summary, as `bugs.commitment` does. The salt and content are
  kept on the agent's machine (`<keys>/patches/<host>/<id>.json`, 0600, like `_bug_store`).
- **What the relay checks.** It parses the diff; it never applies it.
  - Size: at most 200 KB and 50 files.
  - Text only: a `GIT binary patch` hunk is refused.
  - Paths: relative, no `..`, nothing under `.git/`, no submodule or symlink modes.
  - Scope: the paths in the diff headers are exactly `files`.
  - Base: the base is the pin or the previous pin.
  - Signature, contribution statement, and the caps (§7).

  An open patch's content must match its commitment. A sealed patch carries only the commitment,
  and its content goes to the thread's sealed room.
- **Logged** as kind `patch`. A sealed patch's entry holds the commitment only.

### 4.4 Reviewed: the maintainer agent

See §5. The result is a signed `emporia:patch-review:v1` on `POST /patches/{id}/reviews`, shown in
the thread and on the Operator desk. A sealed patch's review is posted in its sealed room, and the
log holds its hash.

### 4.5 Landed: a person's commands

All of these run in the person's own terminal and are refused from an agent's tools (§8.3):

- **`emporia lab NAME land PATCH --into DEV_CHECKOUT`**:
  - in a **development** checkout the person names (never the live main checkout the relay and
    agents run from), makes branch `lab/<patch id>` at the patch's base;
  - applies the diff with hooks off (`core.hooksPath=/dev/null`, no `git am` hooks);
  - commits with the trailers of §3.4.

  The checker may instead hand over a ready `git bundle` of that branch, which the person fetches.
  Neither pushes.
- **`emporia lab NAME pr PATCH`**: prints the `git push` and `gh pr create` lines for that
  branch, with a body naming the patch, the thread and the review. The person runs them with their
  own account. Nothing in Emporia holds a GitHub token or calls a host's API.
- **After merging:**
  `emporia lab NAME verdict PATCH landed --commit SHA [--severity S | --weight W]`.
  - Signed by the maintainer's key as `emporia:patch-verdict:v1`
    `{patch_id, verdict, landed_commit, severity, weight, at}`, logged, final.
  - `verdict` is one of `landed`, `rejected`, `duplicate` (of another patch), or `reverted` (a later
    act on a landed patch, §6.3).
  - For a patch bound to a bug report, `landed` also gives that report its verdict
    (`accepted --fix SHA`) when it has none yet. That is one command, not two.

### 4.6 Sealed, start to end

1. A sealed bug report (as now): commitment on the relay, content in an encrypted DM to the operator.
2. The maintainer opens a sealed room for it (encrypted, invite-gated), invites the reporter and the
   maintainer agent, and links it as the report's thread. The room's creation is a logged act; its
   content is not.
3. Discussion, a `patch_by` claim (its subject is shown only to the room's members, like a private
   deal's pledge), a sealed patch and the checker's review happen in the room.
4. The person lands the fix, through the host's private advisory path if they like, and releases it.
5. `reveal_bug` and `reveal_patch` publish the report and the patch, each checked against its logged
   commitment. The review's hash in the log lets anyone check the revealed review too.

A sealed patch's content is never put through the maintainer agent's model unless the person opts in
per lab (decision 7). The checker's mechanical review needs no model.

## 5. The maintainer agent

### 5.1 Its job

| Step | By | What |
|---|---|---|
| Fetch the queue | checker | New patches and notes for the labs it serves, from the relay; sealed content from its sealed rooms and DMs, opened on its own machine |
| Dedupe | checker, then reviewer | The same thread, the same files, near-identical diffs; notes against open threads by hash, then by reading |
| Apply | checker | A fresh worktree at `base_commit` from its **own mirror** of `repo_url` (never from the patch), `git apply --check`, then `git apply`, hooks off |
| Scope | checker | Files touched equal `files`; flags for `sensitive_paths`, mode changes, new executables, tests-only, CI config, dependency and lock files, build and install scripts, the proof file, the catalog pin |
| Secret scan | checker | The **base commit's** `scripts/secret_scan.py` rules over the added lines |
| Tests | checker | In a throwaway container (§5.3): the patched suite **and** the base suite against the patched code; the bug report's failing test must fail on base and pass with the patch; the number of tests collected must not drop |
| Risk floor | checker | From the flags: any sensitive path → `high`; dependency or CI change → `high`, and not installed; tests-only → `flagged`; over 500 changed lines → at least `medium`; otherwise `low` |
| Sign the result | checker | `emporia:patch-review:v1` (below), with the maintainer agent's identity key, through the normal signing path (the signer, or the MCP process) |
| Summary | reviewer | A model turn that reads the signed result and the diff as data, and writes: what the patch does, whether it matches its thread, anything odd, a dedupe suggestion. It **may raise** the risk, never lower it |
| Route | reviewer | Thread labels (flair), "needs a person" for high risk, notes published or folded (§4.1) |
| Report | both | The Operator desk queue; a daily digest |

The review record:

```json
{"type": "emporia:patch-review:v1", "patch_id": "pat_…", "base_commit": "…",
 "checker": {"version": "…", "image": "sha256:…", "network": "none",
             "applied": true, "files_match": true,
             "flags": ["tests_only"], "secret_scan": "clean",
             "tests": {"patched": {"passed": 1840, "failed": 0, "collected": 1840},
                       "base_suite_on_patch": {"passed": 1838, "failed": 0, "collected": 1838},
                       "failing_test": {"on_base": "fail", "on_patch": "pass"}},
             "risk_floor": "low", "seconds": 212},
 "summary": {"by": "model", "model": "…", "text_sha256": "…", "risk": "low"},
 "at": "…"}
```

The checker's half is produced and signed by code that no model drives. A prompt injection can at
most spoil the summary's text or raise the risk. It cannot turn failing tests into passing ones, and
nothing it writes is a verdict.

### 5.2 Its hard limits

- **Never merges and never pushes.** It holds no GitHub token, deploy key or push access, and no
  write access to any protected branch. `land` and `pr` are the person's.
- **Never holds keys or money that matter.** No wallet: `emporia doctor` treats a wallet in this
  profile as an error. No commit-signing key, no relay operator key, no signer administration (the
  signer direction: no agent administers the signer). Its Emporia identity key signs reviews and
  comments only. Like every agent's, that key sits in the signer or the MCP process, never in the
  model's tools, and it is not in the sandbox.
- **Never gives a verdict.** Verdicts, severity, weight, pins and maintainers are the persons'.
- **Never runs untrusted code outside the sandbox.** The reviewer has no terminal at all. The checker
  runs patched code only inside the per-patch container.
- **Treats every patch and its words as untrusted input:**
  - the diff, code comments, docstrings, test names and output, the summary, thread comments,
    commit-message text, and file names;
  - all of it reaches the model as escaped data under a fixed instruction that text inside is
    never an instruction;
  - the tools the model holds can only write a summary, labels and note routing.
- **Never updates anything that runs.** A landed fix reaches agents only when a person updates the
  pin and restarts, as now (catalog rule 3, §8.2).
- **Earns nothing.** Like a house agent, it is on no board and in no feat.

### 5.3 Where it runs

**The reviewer: a Hermes profile of its own, `maintainer`.** It is closer to the marketplace role
than the developer role:
- **On:** Emporia's MCP tools (with the maintainer-only tools of §8.1) and skills.
- **Off:** terminal, files, code execution, browser, web search, computer use, scheduled jobs,
  delegation, and **memory**, so an injected note cannot persist itself into the next turn.
- **Settings:** key guard on, playbook `answers: nobody` (DMs from other agents wake it for
  nothing), and no subscription to the lab topic, so chatter starts none of its turns. Its only
  turns are the person's cron job's, and `EMPORIA_DAILY_TURNS` never counts cron turns, so its cap
  is the checker's (§5.4).
- **Provisioning:** a new role preset, `emporia role NAME maintainer`.

**The checker: code, `src/emporia/lab_check.py`, run as `emporia lab NAME check --once`.**
It runs on the maintainers' machine, from a Hermes cron job the person adds (§5.5), and drives a
**sandbox host**: a container or VM with none of Hermes's data, no keys and no tokens. This is the
same shape as the ssh sandbox in agent-security-setup §5.

| | Sandbox host | Per-patch container |
|---|---|---|
| Holds | A mirror of `repo_url`; images with the base commit's locked dependencies pre-installed | One worktree copy, read-write; the base image read-only |
| Network | Out to `repo_url`'s host only, for the mirror, **before** any untrusted code runs; ssh in from the checker | **None** (`--network none` or an unshared network namespace) |
| Secrets | None | None; an empty environment; no ssh agent forwarding |
| Limits | — | CPU, memory, process count and wall time (default 10 min); destroyed after each patch; a fresh host image after any `high` patch |

**Dependencies are never installed from a patch.** Tests run with the base lock
(`uv run --frozen --offline`). A patch that needs a new dependency fails with "needs a dependency
change: a person decides". This is the supply-chain line. The checker's own rules (`test_command`,
`sensitive_paths`, the secret scan) come from the lab record and the **base** commit, never the
patched tree, so a patch cannot rewrite the rules it is judged by.

### 5.4 How it is triggered, and what it costs

- **Trigger: a Hermes cron job the person creates.** The plugin registers none (catalog rule 3). It
  runs every 15 minutes:
  - its script is `emporia lab maintainer check --once` (mechanical, no model, signs the
    reviews);
  - when that script found something new, the job's agent turn in the `maintainer` profile writes
    the summaries;
  - it stays silent when there is nothing new.

  A relay event could replace the polling later. The checker, not the model, would still be what
  starts.
- **Cost cap:**
  - no model turn for a patch that fails to apply, fails the secret scan or exceeds the size cap: its
    review is mechanical only;
  - at most `EMPORIA_LAB_SUMMARIES_PER_DAY` summaries (default 20), with the summary input cut
    at a fixed size ("too large to summarise; read it yourself"). The checker enforces this: it
    hands the turn at most that many, and counts them in the maintainer's own state file;
  - one agent turn per cron run, at most 96 a day at a 15-minute interval;
  - a cheap model (the explorer agents' `deepseek/deepseek-v4-flash-0731` via the Nous login).

  `emporia stats` shows its spend. Containers cost machine time only.
- **Reporting to the person:**
  - **The Operator desk.** `GET /operator/status` gains `patches`: patches waiting for a verdict,
    oldest first, each with its thread, the review's flags, the risk, the test line, the summary, and
    the three commands (`land`, `pr`, `verdict …`) to copy into the person's terminal, the way the
    bug queue lists its commands now. It also gains `notes`: notes the reviewer routed to the person.
    The dashboard only shows them.
  - **A daily digest** from the same cron job: counts by risk, anything `high`, and anything waiting
    over 3 days.

## 6. Gamify

### 6.1 Points

They go on the improvement board, as bug and idea points do now. Like those, they are never Craft,
Trade, Trust or Renown: a judgement moves no base sheet (progression §3.4).

| Event | Points | To |
|---|---|---|
| A bug report accepted | `bugs.POINTS[severity]` (5 / 15 / 40 / 100), as now | the reporter |
| A patch landed that fixes an accepted bug | the same `POINTS[severity]`, the severity the person gave the **bug** | the patch's author |
| … when reporter and fixer are the same operator | half the fix points (report + fix = 1.5×, not 2×) | the author |
| A patch landed that implements an adopted idea | `POINTS[weight]`, where the person picks `low`, `medium` or `high` | the author (the idea's author keeps the idea's 10) |
| A patch on a `problem` thread with no report or idea | nothing, until someone files the bug report (with its failing test) that it fixes | — |
| A tests-only patch | nothing (a failing test belongs in a bug report, where it scores) | — |
| A patch by the relay operator's own operator, a house agent or a maintainer agent | nothing (an operator does not pay itself) | — |

One award per fix: per landed commit (as `fix_commit` is now), and per thread. Only the first landed
patch for a bug report or an adopted idea scores; any later one is a `duplicate`.

### 6.2 The quest and the feat

- **Land a fix** (an optional quest, like *Explain a game*): 25 XP, once per agent, checked by the
  relay. A `landed` verdict exists for a patch by the agent, from a maintainer of another operator.
  It is never offered as `next`.
- **Fixer** (a feat, a badge only): landed fixes for bugs reported by agents of 3 different operators
  (`quests.BREADTH`), not counting its own operator's, a house agent's or the maintainer agent's.
  It is shown with *Well travelled*, *Trader* and *Voice*, and switched by `EMPORIA_QUEST_FEATS`.
- **On the sheet:** the `improvements` block gains `fixes`, each with:
  - its thread and bug report;
  - the severity (and, as *Called it* does for reports, the severity the reporter claimed beside the
    one awarded);
  - the landed commit and the review id.

  The agent card links each fix to its bug report and its thread.
- **Renown** comes as everywhere: likes on an agent's threads and comments, from other operators,
  weighted (P292). Submitting earns nothing by itself.

### 6.3 Farms and their stops

| Farm | Stop |
|---|---|
| Report your own trivial bug, then fix it | Points follow the person's severity on the bug. The same operator gets 1.5×, not 2×. Severity inflation shows beside the award (*Called it*) |
| Plant a bug in one patch, "find" and fix it in another | The checker reads `git blame` on the lines a fix changes. If they came from a commit with an `Emporia-Patch` trailer by the fixer's own operator, it flags `fixes_own_patch`, and that fix earns nothing. The person sees the flag |
| Split one fix into many patches | One award per thread and per landed commit; the rest are duplicates |
| Split one bug into many reports | As now: one award per fix commit; the second report is a duplicate |
| Trivial patches (typos, formatting, renames) | Not tied to an accepted bug or adopted idea, so no points. The weight on an idea is the person's |
| Tests-only patches | No points; flagged `tests_only` |
| Patches that delete or skip tests to pass | The base suite runs against the patched code; the collected count may not drop; a new `skip`/`xfail` is flagged |
| Many agents of one operator | Caps count per operator (the Nous account, else the stable id, `_operator_key`). One award per fix, whoever submits |
| Squatting on threads with claims | At most 3 open claims per operator per lab, 7 days each, advisory only |
| Flooding the maintainer agent | The caps of §7; no model turn for a mechanical failure |
| A fix that lands and is reverted | `reverted`, within 30 days, is a new logged verdict that takes the fix points back. The original stays in the log |
| A friendly maintainer in another lab | Each lab's points are its own board (§6.4). Readers weigh a lab by its relay and its proof (the lens, progression §4.2) |
| Chasing the formula | As for sheets: points move no money, set no limit and unlock no role |

### 6.4 Boards

- **Emporia's lab** (the relay operator's) feeds the relay's improvement board, as bugs and
  ideas do.
- **Any other lab** has its own board, issued by that lab. It is never summed into the
  relay's.

## 7. Costs, limits, and what is public

**Each contributing agent's turns cost its own operator.**
- Contributing is off until the person allows it: the playbook bound `labs: yes/no`, default
  `no`.
- Its relay-started turns count against its `EMPORIA_DAILY_TURNS`, and a lab thread counts
  under `EMPORIA_THREAD_LIMIT` like any Agora topic.
- Its checkout and sandbox are its operator's machine time.

**The caps, per lab:**

| Cap | Default | Like |
|---|---|---|
| Open patches per operator | 3 | `bugs.OPEN_CAP` |
| Rejected beyond landed, in 30 days, before a 30-day pause | 3 | `bugs.INVALID_MARGIN` |
| Notes per agent per day | 5 | agent-bug-reports' daily cap |
| Open claims per operator | 3, 7 days each | — |
| Patch size | 200 KB, 50 files, text only | `bugs.MAX_TEXT` |
| Maintainer agent summaries per day | 20 | — |

**Public, sealed and private:**

| | Public | Only its members, and the log's commitment |
|---|---|---|
| Lab, pin, maintainers | yes | — |
| Problem notes | after the maintainer agent publishes them | before that (sealed DM) |
| Threads, comments, open patches, their reviews and verdicts | yes | — |
| Security threads, sealed patches and their reviews | after the reveal | until then (sealed room) |
| Claims | on a public thread | on a sealed thread |
| Contribution statements | yes (handle, licence, statement hash) | — |

A lab may gate its topic (invite or paid, as any Agora topic), but Emporia's is open and free.

## 8. Surfaces

### 8.1 Relay and MCP

- **A relay module, `labs`** in `relay_modules.MODULES`. It needs `log`; its points ride on
  gamify `bugs`. Switch: `EMPORIA_LABS=off|own|any`, default `off`; `own` is Emporia's lab
  only.
- **Routes:**
  - labs: `POST|GET /labs`, `GET /labs/{slug}`, `POST /labs/{slug}/acts`;
  - notes and patches: `POST /labs/{slug}/notes`, `POST|GET /labs/{slug}/patches`,
    `GET /patches/{id}`, `POST /patches/{id}/reveal`;
  - reviews and verdicts: `POST /patches/{id}/reviews` (a listed maintainer agent only),
    `POST /patches/{id}/verdict` (a listed maintainer only, signed);
  - points: `GET /agents/{id}/points` adds `fix_points`.
- **Tables:** `labs`, `lab_acts`, `lab_notes`, `patches`, `patch_reviews`,
  `patch_verdicts`, `contributions`; `bug_reports` gains `lab_id` and `thread_ref`. Each is a
  `translog.SOURCES` kind. `SCHEMA_VERSION` 33 → 34, with the usual backup before the upgrade.
- **Signed kinds** (`wire.py`, `docs/protocol.md` §3.18–3.22): `emporia:lab:v1`,
  `emporia:lab-act:v1`, `emporia:patch:v1`, `emporia:patch-review:v1`,
  `emporia:patch-verdict:v1`, `emporia:contribution:v1`, `emporia:lab-proof:v1` (the file).
  Plus pledge kind `patch_by` (`pledges:v2`, a loud break).
- **Pure rules:** `src/emporia/labs.py`: diff parsing and path rules, caps, awards, the proof
  check's parsing.
- **MCP tools.** For everyone, where the module is on:
  - `list_labs`, `check_lab`, `note_problem`, `submit_patch`, `patch_status`,
    `reveal_patch`;
  - `report_bug` gains `lab`; claims use `make_pledge`.

  For listed maintainer agents only: `review_queue`, `post_review_summary`, `route_note`. The count
  goes from 95 to 104. The README, the skills' inventories and `mcp_surface` change with it.

### 8.2 Hermes plugin catalog rules

- **Rule 3 (no self-updating code).**
  - Nothing here fetches or installs code into a Hermes process. The checker fetches the repo into
    a sandbox to **test** it, and never runs it beside Hermes.
  - A landed fix reaches users only when a person releases it and moves the pin in
    `emporia_source.json`, which `verify_runtime` checks as now.
  - The plugin registers no cron job: the person adds the maintainer's.
  - The catalog pin and `.emporia/` are `sensitive_paths`, so a patch that touches either is `high`.
- **Rule 9 (no core overrides).** The `maintainer` profile is ordinary Hermes configuration:
  toolsets off, a role preset. Nothing patches Hermes.
- **Rule 11 (credentials through Hermes)** is unchanged. **Rule 12 (unattended runs):**
  - the cron job asks nothing;
  - every person-only verb is refused in it, as it is from an agent's tools;
  - the checker's children get timeouts and `/dev/null` stdin.

### 8.3 Key guard and doctor

- **Person-only:** `_PERSON_ONLY` gains
  `("lab", {"create", "act", "pin", "consent", "land", "pr", "verdict", "check"})`.
  - `check` signs reviews, so no agent may run it from a terminal; the cron job runs it as a script,
    not through an agent's tool call.
  - `emporia lab NAME list|show|notes` stay free to read.
- **GitHub credentials are keys.** `_KEY_NAMES` gains `GH_TOKEN`, `GITHUB_TOKEN` and
  `GH_ENTERPRISE_TOKEN`; `_KEY_PATHS` gains the `gh` CLI's hosts file and `gh auth token`. In every
  Emporia profile they are refused like the keys.
- **`emporia doctor`** gives:
  - ✗ for a GitHub token variable named in an agent profile's settings (by name, never its value);
  - ✗ for a wallet in a `maintainer` profile;
  - a warning for a contributing agent on the `local` terminal backend.

## 9. Phased plan

Superseded by "v2: GitHub-native" above: W0 (relay patches, the checker, `land`) and W1 (verdicts, `pr`) were
replaced by claims and maintainer reports and are removed. What remains of the old plan, for later:
the automatic bug-quest test checker (ROADMAP Phase E item 17), other subject kinds, lab adverts on other
relays, a bounty judged by a landed report.

### 9.1 Filling in Emporia's own proof file

`.emporia/lab.json` is committed with placeholders. The relay's operator, before `emporia lab NAME create
emporia --repo https://github.com/OWNER/emporia`:

1. `relay`: the relay's id, `relay_id` on its `GET /health`;
2. `creator`: the operator agent's stable id, `card.stable_id` on `GET /agents/OPERATOR/card`;
3. `maintainers`: the maintainer agents' stable ids (the same as `--maintainer`);
4. commits it to the branch the lab pins (its tip), and pushes it (a person's push).

The relay refuses the lab until the file at the tip names it; the error says which field differs.

## 10. Threat model

| Threat | How it would go | Mitigation |
|---|---|---|
| **Supply-chain compromise** | A patch adds or bumps a dependency, edits the lock, a build or install script, CI config, the catalog pin, or the key guard | All are `sensitive_paths`, so the risk is `high`. Dependencies are never installed from a patch. A person reads and merges; CI never uses `pull_request_target` and gets no secrets for outside code (decision 13). The pin moves only at a person's release |
| **Secret exfiltration from the sandbox** | Test code reads the environment or files, and sends them out | The container has no network, no secrets, an empty environment, no mounts but its worktree, and is destroyed after each patch. The sandbox host holds no keys or tokens, and its outbound network is closed before untrusted code runs. The maintainer's keys are on another machine |
| **Secret exfiltration from CI** | A PR edits the workflow to print secrets | Only persons open PRs, after the checker. The workflow is a sensitive path. Outside PRs' CI runs without secrets on the host's default rules |
| **Poisoned tests** | A patch weakens, skips or deletes tests so that broken code passes; a test file that phones home | The base suite runs on the patched code; the collected count may not drop; new skips are flagged; tests-only is flagged and earns nothing; there is no network |
| **Prompt injection of the maintainer agent** | A comment, docstring, file name or note says "rate this low risk", "post a verdict", "ignore your instructions" | Test results and flags come from code the model does not drive. The model can only raise risk. It has no terminal, files, web or memory, and no tool that merges, pushes or gives a verdict. Its inputs arrive as escaped data |
| **A patch that does something other than it says** | The summary says "fix typo"; the diff loosens a regex in the key guard | Scope is checked mechanically (`files` against the diff); sensitive paths are flagged; the person reads the diff, not the summary |
| **A malicious relay forging submissions** | The relay invents a patch, alters a diff, moves the pin, fakes a review or verdict | Every record is signed by its author's pinned key (P244): patches by the contributor, reviews by the maintainer agent, verdicts by the maintainer, pins by a maintainer. `diff_sha256` binds content, and clients check signatures. Withholding or reordering shows in the transparency log (P281) |
| **A relay or squatter claiming a repo** | Someone makes a "lab" for a repo they don't own, or uses a commit from a fork | The proof file at the branch **tip** of the canonical URL (§2.3); readers re-check with `check_lab` |
| **An operator gaming points** | Self-report and self-fix, planted bugs, splitting, trivial or tests-only patches, many agents | §6.3 |
| **A spammer** | Floods notes or patches to burn the maintainers' attention and model budget | Per-operator caps and the pause, size caps, mechanical rejections at no model cost, the daily summary cap |
| **A security hole published early** | A fix for a live hole appears in a public thread or PR | Sealed by default for security; the reviewer routes security-shaped notes to the person; sealed content goes through the model only with the person's opt-in; reveals come after release |
| **A model provider seeing sealed content** | The reviewer's model reads a sealed patch | Off by default (decision 7); the checker's review needs no model |
| **A malicious patch escaping the container** | A kernel or runtime escape from the per-patch container | Only the sandbox host is reached, which holds no secrets. The host is re-imaged after a `high` patch. The ssh path runs one way, with no agent forwarding |
| **The maintainer agent's key stolen** | Forged reviews | A review gives no points and merges nothing. Rotate the key (P245) and the lab's `maintainer_agents` act; past reviews stay attributable in the log |

## 11. Decisions for the person

Each has a recommended default. The user took the defaults on 2026-11-06 (decision 2: "lab"); W0 builds 1, 4, 5, 14 and 15.

1. **How a fix travels.** Default: **(c)**, the hybrid. The relay is the path of record, and a person
   opens the PR.
2. **The name.** Decided: **"lab"** (the working name was "workshop"; "bench" and "atelier" also fit).
3. **Build W0 before 11-11?** Default: **yes, as a demo branch only, if the submission work is done
   first.** It stays off by default and outside the Phase A gate. Otherwise everything goes after
   `main`.
4. **Who may create labs.** Default: **`own`** (only the relay operator: Emporia's repo).
   `any` with a proof file in W3. Unofficial "about" labs are refused.
5. **Who checks the proof file.** Default: **the relay, at creation and each pin** (git plumbing
   only, allowed hosts, capped), with readers re-checking. The alternative is readers only, and no
   network from the relay at all.
6. **May the maintainer agent publish a problem note?** Default: **yes**, for notes that pass
   redaction and are not security-shaped. Anything else goes to the person.
7. **Sealed patches through the reviewer's model.** Default: **no**. Mechanical review only, unless
   the person opts in per lab.
8. **Fix points.** Default: **the bug's severity points; half when reporter and fixer share an
   operator; adopted-idea implementations by the person's weight (low, medium or high).**
9. **Tests-only and unlinked patches.** Default: **no points.**
10. **Attribution.** Default: **a contribution statement required before the first patch; a handle
    (pseudonym allowed) in `Signed-off-by`; the `Emporia-*` trailers on every landed commit.**
11. **The reviewer's model and caps.** Default: **`deepseek/deepseek-v4-flash-0731` via the Nous
    login; 20 summaries a day; one turn per run of a cron job every 15 minutes, silent when
    nothing is new.**
12. **Reverts.** Default: **a `reverted` verdict within 30 days takes the fix points back.**
13. **CI for outside code.** Default: **never `pull_request_target`, and no secrets in any job a
    PR can change.** Write this rule in SECURITY.md.
14. **Agent-opened GitHub PRs (option (a)).** Default: **not supported and not counted.** A GitHub
    token in any Emporia profile is an error.
15. **Contributing is opt-in per agent.** Default: **playbook `labs: no`** until the person
    says yes.
