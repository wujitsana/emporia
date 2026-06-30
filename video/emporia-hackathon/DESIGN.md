# Emporia hackathon video — visual identity

## Style Prompt

Nous-adjacent **retro-modern terminal**: late-90s operator-console nostalgia, 2026 flat SRCL UI. Film grain light, amber phosphor accent, monospace authority. Motion is **technical** (precise, not bouncy SaaS).

## Colors

| Role | Hex |
|------|-----|
| Canvas | `#0a0a0a` |
| Raised surface | `#141414` |
| Primary text | `#e8e6e3` |
| Muted | `#6b6b6b` |
| Accent (amber) | `#f0a832` |
| Accent dim | `#a67420` |
| Scanline / grid | `#1a1a1a` at 40% opacity |

## Typography

- **Display / lower-thirds:** `"Commit Mono", "IBM Plex Mono", ui-monospace, monospace`
- **Captions:** same family, weight 500–600, tutorial sizing (48–56px landscape)

## Motion

- Entrances: `power2.out`, 0.35–0.5s
- Chapter cards: short scale 0.96→1 + opacity
- Interstitials: 1.2–2.0s total; one shader or CSS transition only per project
- No infinite loops; finite GSAP repeats only

## Imaging (retro-modern edits)

Between browser segments, use **full-frame interstitials** (not PiP):

1. **Grain + vignette** overlay on black (CSS pseudo-layer)
2. **Amber horizontal sweep** (1px line animates top→bottom, 0.6s)
3. **Chapter typography** — one line, e.g. `NOUS_VERIFIED`, `INBOUND CONTRACT`
4. Optional **chromatic split** or **flash-through-white** at 15% opacity (HyperFrames `add` shader) — use max 3× in full video

Browser footage stays **clean** (no grain on live UI); grain only on interstitials and intro/outro.

## What NOT to Do

- No startup blue `#3b82f6`, no Roboto/Inter marketing fonts
- No gradient mesh heroes, no stock “AI brain” imagery
- No jump cuts chapter→chapter without interstitial or transition
- No ASCII filter on dashboard (hurts judge readability)
- No covering WS feed or audit badge with captions — safe area bottom 120px only on browser tracks

## Reference UI

Live reference: `http://localhost:8088/ui/` — `theme-dark`, `✶ Emporia`, pipeline strip, amber focus.