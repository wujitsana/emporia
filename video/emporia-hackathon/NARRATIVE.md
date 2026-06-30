# Emporia hackathon — narrative & picture edit

**Format:** 1920×1080, ~5:30 target, 30fps  
**Engine:** HyperFrames compositor + OBS browser plates  
**Mood:** Retro-modern interstitials **between** clean SRCL dashboard scenes.

---

## Story (one arc)

Agents already run businesses in code — but they can’t open ports, trust anonymous peers, or settle stakes without a shared floor. **Emporia** is that floor: a relay Hermes agents hit outbound-only. Identity is cryptographic; commerce is Stripe on the relay; safety is guardrails + proof-of-reasoning before any move lands.

The video **alternates**:

- **A — Cinematic layers** (intro, chapter cards, imaging edits) = retro-modern  
- **B — Browser truth** (live `/ui/` + one CLI beat) = modern, sharp  

Judges remember **B**; **A** gives pacing and Nous aesthetic.

---

## Beat sheet (timecodes are targets after VO record — adjust in HyperFrames)

| Seq | Type | Duration | Visual | VO (see SCRIPT.md) |
|-----|------|----------|--------|---------------------|
| 0 | **INTRO** | 0:00–0:12 | Black → grain → `✶ EMPORIA` kinetic mono → amber line sweep → subline “federated agent commerce relay” | Lines 1–2 |
| 1 | **I-edit** | 0:12–0:15 | Interstitial: `OUTBOUND ONLY` full frame | — (music bed or silence) |
| 2 | **BROWSER** | 0:15–0:55 | Overview: pipeline strip, hub, live feed, safety + fees panels | Lines 3–5 |
| 3 | **I-edit** | 0:55–0:58 | Interstitial: `INBOUND CONTRACT` + mini pipeline text | — |
| 4 | **BROWSER** | 0:58–1:25 | Agents: nous vs key, MCP hint | Lines 6–7 |
| 5 | **I-edit** | 1:25–1:28 | Interstitial: `ED25519 · JWKS` | — |
| 6 | **BROWSER** | 1:28–2:05 | Sessions: chess board, audit `✓ chain verified` | Lines 8–9 |
| 7 | **I-edit** | 2:05–2:08 | Interstitial: `PROOF-OF-REASONING` amber pulse | — |
| 8 | **BROWSER** | 2:08–2:35 | PoR rejection → cut Overview counters tick | Lines 10–11 |
| 9 | **I-edit** | 2:35–2:38 | Interstitial: `NEMO · GUARDRAILS` | — |
| 10 | **BROWSER** | 2:38–3:05 | Fees: escrow, 2.5% / 97.5% | Lines 12–14 |
| 11 | **I-edit** | 3:05–3:08 | Interstitial: `STRIPE ESCROW` | — |
| 12 | **BROWSER** | 3:08–3:25 | Quick: Rooms + Agoras scroll (optional) | Line 4b optional |
| 13 | **B-edit** | 3:25–3:40 | Hermes CLI plate: registration line `nous_verified` (mono terminal) | Line 15a |
| 14 | **OUTRO** | 3:40–4:00 | Retro outro: repo path, `DEMO.md`, Hermes × NVIDIA × Stripe × Nous logos as **text** only | Line 15 |

**Stretch to ~5:30** by holding Overview feed + one Agoras row longer, not by more interstitials.

---

## Imaging edit vocabulary (reuse across I-edit slots)

Each interstitial uses the **same CSS kit** (one composition, different `data-text` props or duplicate scenes):

1. Base: `#0a0a0a` + animated film grain (CSS background, seeded noise tile)
2. Vignette: radial gradient overlay
3. Center chapter line: Commit Mono, amber, letter-spacing +2px
4. Sweep: 1px `#f0a832` line, `y` from -10% → 110% in 0.5s
5. Exit: `flash-through-white` at low opacity OR 8-frame white flash in GSAP (if no shader)

**Transition into browser:** shader `liquid-wipe` or CSS blur crossfade 0.4s — browser track fades in sharp (no grain).

**Transition out of browser:** 0.3s dip to black at end of each B segment in OBS (or trim in HyperFrames) so interstitial reads as intentional.

---

## Production files (you will create)

```
emporia/video/emporia-hackathon/
  DESIGN.md              ← done
  NARRATIVE.md           ← this file
  SCRIPT.md              ← VO lines
  recordings/
    intro-outro/         ← HyperFrames renders (compositions only)
    browser/
      01-overview.mp4
      02-agents.mp4
      03-sessions.mp4
      04-por-safety.mp4
      05-fees.mp4
      06-rooms-agoras.mp4   (optional)
      07-hermes-cli.mp4
    narration.wav           ← VO master
  compositions/
    intro/
    interstitial/           ← one template, chapter text variants
    outro/
  index.html                ← HyperFrames master timeline (after init)
```

---

## OBS recording rules (browser plates)

- One file per **B** row above (easier to re-cut than one 5min take)
- 1920×1080, cursor visible, no OS notifications
- Before each segment: 0.5s hold on first frame (helps transitions)
- PoR segment: show terminal or Hermes tool result **then** Overview counter — can be two clips merged in HF

---

## HyperFrames assembly order

1. `setup.sh` + `doctor`
2. Author `compositions/intro`, `compositions/interstitial`, `compositions/outro` (hero frame first, per skill)
3. Record all `recordings/browser/*.mp4` + `narration.wav`
4. `npx hyperframes init . --non-interactive` (if not already)
5. Master timeline: alternate **video tracks** — Intro comp → Browser 01 → Interstitial comp (props) → Browser 02 → … → Outro
6. Caption track from `npx hyperframes transcribe narration.wav` (tutorial style per DESIGN.md)
7. `lint --strict && validate && inspect`
8. `render --quality draft` → review narrative pacing
9. `render --quality high --output emporia-hackathon-final.mp4`

---

## Next actions (pick up in order)

- [ ] **1.** Bootstrap relay + seed; QA `/ui/` (`emporia-dev`)
- [ ] **2.** Record `narration.wav` from SCRIPT.md (or TTS later)
- [ ] **3.** Build intro + interstitial + outro compositions (HyperFrames)
- [ ] **4.** Record browser plates per table (OBS)
- [ ] **5.** Assemble master `index.html` timeline with transitions
- [ ] **6.** Draft render → final render → upload + README link

Provider for agent help on steps 3–5: **xai-oauth** or **nous** only; recording steps are human/OBS.