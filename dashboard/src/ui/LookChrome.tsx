/**
 * The look's moving parts (look.ts, dashboard.css § 30): the header's brand and
 * the section heading decode from scrambled glyphs, the line under the heading
 * types out with a block cursor, and two status lines frame the view.
 *
 * Motion is short and only on arrival: the brand decodes from the demo's title
 * card (`E#P0R!A` → `EMPORIA`) once when the dashboard opens and, more briefly,
 * when a section opens; the heading resolves in about 0.4 s and its line types
 * in about 0.6 s. With `prefers-reduced-motion`, or in capture mode, all of them
 * are shown finished.
 */
import { useEffect, useRef, useState } from "react";
import { VIEWER_ID } from "../relayEnv";
import { DESTINATIONS } from "../navConfig";
import { motionAllowed, scrambleFrame } from "../look";

const SCRAMBLE_MS = 420;
const TYPE_MS_PER_CHAR = 9;
const TYPE_MAX_MS = 650;

/** power2.out: fast, then settling — the animatic's easing. */
const ease = (t: number) => 1 - (1 - t) * (1 - t);

/**
 * Milliseconds since `key` arrived, counting up to `ms` and then holding there.
 * `ms` straight away when not moving. A new key reads 0 on its first render, so
 * an entrance never shows its end state first.
 */
function useArrival(key: string, ms: number, moving: boolean, stepMs = 0): number {
  const [tick, setTick] = useState<{ key: string; at: number }>({ key: "", at: 0 });
  useEffect(() => {
    if (!moving) return;
    const h = { raf: 0, last: 0 };
    const t0 = performance.now();
    const loop = (now: number) => {
      const at = now - t0;
      if (at >= ms) {
        setTick({ key, at: ms });
        return;
      }
      if (now - h.last >= stepMs) {
        h.last = now;
        setTick({ key, at });
      }
      h.raf = requestAnimationFrame(loop);
    };
    h.raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(h.raf);
  }, [key, ms, moving, stepMs]);
  if (!moving) return ms;
  return tick.key === key ? tick.at : 0;
}

/** A heading that decodes from scrambled glyphs into its word, once per text. */
export function ScrambleText({ text }: { text: string }) {
  const moving = motionAllowed();
  // A new glyph every ~3 frames reads as decoding, not noise.
  const at = useArrival(text, SCRAMBLE_MS, moving, 45);
  if (!moving || at >= SCRAMBLE_MS) return <>{text}</>;
  return (
    <>
      <span aria-hidden>{scrambleFrame(text, Math.floor(ease(at / SCRAMBLE_MS) * text.length))}</span>
      <span className="e-sr">{text}</span>
    </>
  );
}

/** The brand as the demo's title card first shows it, before it resolves. */
export const BRAND = "EMPORIA";
export const BRAND_SCRAMBLED = "E#P0R!A";
/** The first decode, when the dashboard opens; a section switch replays a shorter one. */
export const BRAND_OPEN_MS = 900;
export const BRAND_REPLAY_MS = 320;

/**
 * The header's EMPORIA, in the pixel face. It opens on `E#P0R!A` and decodes left
 * to right (the video's title card), once on load; each `replay` change (a section
 * opening) runs a short, gentler decode. Still, finished, under reduced motion and
 * in capture mode. Screen readers read the word, never the glyphs.
 */
export function BrandTitle({ replay = "" }: { replay?: string }) {
  const moving = motionAllowed();
  const first = useRef(true);
  const [run, setRun] = useState<{ key: string; ms: number }>({ key: `open:${replay}`, ms: BRAND_OPEN_MS });
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setRun({ key: `replay:${replay}`, ms: BRAND_REPLAY_MS });
  }, [replay]);
  const at = useArrival(run.key, run.ms, moving, 55);
  let shown = BRAND;
  if (moving && at < run.ms) {
    const opening = run.ms === BRAND_OPEN_MS;
    // A replay is gentler: the first two letters stay, only the rest flickers back.
    const from = opening ? 0 : 2;
    const resolved = from + Math.floor(ease(at / run.ms) * (BRAND.length - from));
    // The opening run starts on the title card's own frame.
    shown = opening && at < 160 ? BRAND_SCRAMBLED : scrambleFrame(BRAND, resolved);
  }
  return (
    <span className="e-brand" data-decoding={shown !== BRAND ? "true" : undefined}>
      <span aria-hidden>{shown}</span>
      <span className="e-sr">Emporia</span>
    </span>
  );
}

/** A line typed out with a block cursor after it; the cursor stays, blinking. */
export function TypedText({ text }: { text: string }) {
  const moving = motionAllowed();
  const per = Math.min(TYPE_MS_PER_CHAR, TYPE_MAX_MS / Math.max(1, text.length));
  const total = per * text.length;
  const at = useArrival(text, total, moving);
  const n = at >= total ? text.length : Math.floor(at / per);
  return (
    <>
      {n < text.length ? (
        <>
          <span aria-hidden>{text.slice(0, n)}</span>
          <span className="e-sr">{text}</span>
        </>
      ) : text}
      <span className="e-anim-cursor" aria-hidden />
    </>
  );
}

const OPENED = Date.now();

function clock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h ? `T+${h}:${mm}:${ss}` : `T+${mm}:${ss}`;
}

/** The status line under the view: time since this dashboard opened, who it views as, and that it only watches. */
export function AnimaticHudFoot({ section }: { section: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="e-anim-hud e-anim-hud--foot" aria-hidden>
      <span>{clock(now - OPENED)} · {VIEWER_ID ? `as ${VIEWER_ID}` : "spectator"}</span>
      <span>{section} · watch only · keys 1–{DESTINATIONS.length} · [ ] pages · t T look</span>
    </div>
  );
}
