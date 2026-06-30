# Hackathon presentation video (Emporia / emporia)

Condensed operator guide: skills, providers, aesthetic, narrative edit, and canonical tour. Full task breakdown may live in `.hermes/plans/*-hackathon-presentation-video.md`.

## Canonical tour script

**Source of truth for on-screen path:** `emporia/DEMO.md` Step 2 (dashboard-first guided tour).

**Pre-flight:**

```bash
cd emporia
.venv/bin/python installer/install.py --bootstrap-test
# or relay + seed: see references/demo-relay-seed.md (EMPORIA_GAMES_DB alignment)
open http://localhost:8088/ui/
```

**Target length:** 4–7 minutes. Hero = live `/ui/`; one MCP write beat (PoR rejection); short Hermes CLI registration line.

## Narrative + retro-modern edits (HyperFrames)

For **intro / chapter interstitials / outro / captions** between **OBS browser plates**, load **`hyperframes`** (hub skill — do not patch; follow its SKILL.md + `references/website-to-video.md`).

**Hybrid pattern:**

| Layer | Tool | Notes |
|--------|------|--------|
| Live proof | OBS → `video/emporia-hackathon/recordings/browser/*.mp4` | One clip per DEMO beat; sharp UI, no grain |
| Cinematic beats | HyperFrames compositions | Grain/vignette/amber sweep **only** on interstitials |
| VO + captions | See **AI narration** below | `narration.wav` drives `hyperframes transcribe` |

**Do not** use `hyperframes capture` of `localhost:8088/ui/` as the main video — static capture misses WS counters, chess socket, safety bumps.

**Repo scaffold (in-tree):** `emporia/video/emporia-hackathon/` — `DESIGN.md`, `NARRATIVE.md` (alternating **retro interstitials** ↔ **clean browser** OBS plates), `SCRIPT.md`, `README.md`, `recordings/TTS_TEST.md`.

**Narrative arc:** Intro (grain/amber kinetic type) → short chapter cards (`OUTBOUND ONLY`, `INBOUND CONTRACT`, `PROOF-OF-REASONING`, …) between DEMO §2a–2e browser clips → Hermes CLI plate → outro (`DEMO.md`). Browser footage stays ungraded; grain only on HyperFrames A-scenes.

**HyperFrames render prep:** `npx hyperframes doctor`; `npx hyperframes browser ensure` if headless Chrome missing; use `render --docker` when `/dev/shm` is tiny.

## AI narration (Hermes — not a separate skill)

Use the agent **`text_to_speech` tool** only (no shell scripts for VO generation unless the user explicitly wants ffmpeg). There is no dedicated “TTS skill” in the catalog. The agent **cannot** patch `config.yaml` (security) — user runs `hermes config set …` for voice/provider changes.

**Try in this order:**

| Step | Provider | Cost | How |
|------|----------|------|-----|
| 1 | **Edge** (default) | $0 | `tts.provider: edge`; call `text_to_speech` with `recordings/narration.txt` → `output_path` under `video/emporia-hackathon/recordings/` |
| 2 | **OpenAI voices via Nous Tool Gateway** | Portal / tool pool | **Not** a personal `OPENAI_API_KEY` — see [Tool Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/features/tool-gateway.md). Managed vendor `openai-audio` uses Nous OAuth + `tool_gateway_entitled`. |
| 3 | HyperFrames Kokoro | Local | `npx hyperframes tts narration.txt` after `pip install kokoro-onnx soundfile` |

**Edge voices (documentary):** `en-US-GuyNeural`, `en-US-ChristopherNeural`, `en-US-DavisNeural` — user: `hermes config set tts.edge.voice en-US-GuyNeural`.

**Nous gateway OpenAI TTS (premium VO):**

```bash
hermes config set tts.provider openai
hermes config set tts.use_gateway true
hermes config set tts.openai.model gpt-4o-mini-tts
hermes config set tts.openai.voice onyx   # or ash, cedar
```

Then short `text_to_speech` test → full script. On failure, error should reference **managed OpenAI audio** / `hermes model` — not “set OPENAI_API_KEY”. BYOK only if user sets `VOICE_TOOLS_OPENAI_KEY` and `tts.use_gateway: false`.

**Artifacts:** `narration.txt` → `narration_edge.mp3` (or `narration_openai.mp3`) → convert to `narration.wav` (48 kHz mono) for HyperFrames. In-repo checklist: `video/emporia-hackathon/recordings/TTS_TEST.md`.

**Captions:** `npx hyperframes transcribe recordings/narration.wav` after VO is locked.

## Judge tracks → dashboard evidence

| Track | Show on screen |
|--------|----------------|
| **NVIDIA / NeMo** | Overview pipeline strip; Trust & Safety counters; Sessions audit chain badge; live counter bump after failed `submit_action` |
| **Stripe** | Overview/Fees panel (2.5% fee, escrow narrative); Fees breakdown |
| **Nous / decentralized** | Agents `nous_verified`; Federation panel; Hermes `[emporia] Registered … trust: nous_verified` |

## Hermes skills to load

**Required for prep + record:**

| Skill | Role |
|--------|------|
| `emporia-dev` | Bootstrap, DEMO, seed, this reference |
| `srcl-terminal-ui` | Dashboard QA before record; retro-modern = Sacred tokens |
| `emporia` | Live PoR rejection via MCP `submit_action` |
| `hermes-agent` | Profiles, `/reload-mcp` (bundled — do not patch) |
| `hyperframes` | Intro/interstitials/outro, captions, final mux (hub — read-only) |

**Optional creative (match dark terminal, not SaaS templates):**

| Skill | Use |
|--------|-----|
| `architecture-diagram` | 5–10s dark SVG pipeline b-roll from `ARCHITECTURE.md` |
| `claude-design` / `sketch` | HTML title card: `✶ Emporia`, amber `#f0a832`, CommitMono |
| `humanizer` | Tighten VO script |
| `dogfood` | Pre-record browser QA on `/ui/` |

**Skip for Nous retro-modern look:** `manim-video`, `baoyu-infographic`, `popular-web-designs`; full-screencast `ascii-video`.

**Name in narration (not required to run during edit):** `stripe-link-cli`, `mpp-agent`.

## Providers (agent work while drafting)

- **One LLM provider for prep:** user's default (e.g. `xai-oauth`) until quota low, then **Nous** — avoid OpenRouter/MOA for long agent loops during video prep.
- **VO:** `text_to_speech` + **Edge** = $0; gateway OpenAI TTS bills via **Nous Tool Gateway**, not chat tokens.
- **Auxiliary `auto`:** needs `OPENROUTER_API_KEY` or `GOOGLE_API_KEY` only if using vision on frame grabs.

## Pitfalls (agent)

- Do **not** instruct users to use raw `OPENAI_API_KEY` for hackathon narration when they cited Nous Portal — use `tts.use_gateway` + Tool Gateway docs.
- Prefer **`text_to_speech`** over terminal for VO; avoid blocked/dangerous shell probes when tools suffice.
- Do **not** patch bundled `hermes-agent` or hub **`hyperframes`** skills — read-only; patch this reference or in-repo `video/` scaffold only.

## Nous retro-modern aesthetic

The submission look **is** the Emporia SRCL dashboard:

- `theme-dark`, CommitMono, accent `#f0a832`, flat canvas, square tabs
- Hermes insert: dark terminal, same palette
- Interstitials: mono chapter type + light grain; browser plates stay clean

See **`srcl-terminal-ui`** → `references/emporia-dashboard-cards-minimal.md`, `emporia-dashboard-flat-canvas.md`.

## PoR rejection beat (rehearse once)

```text
mcp_emporia_submit_action(session_id=<live chess id>, action_type="move",
  payload={"move": "d2d4"}, rationale="ok")
```

Expect 403; Overview Trust & Safety counters increment.

## Repo docs to cite

- `emporia/README.md` — hackathon judge table
- `emporia/DEMO.md` — tour steps
- `emporia/ARCHITECTURE.md` — inbound order diagram