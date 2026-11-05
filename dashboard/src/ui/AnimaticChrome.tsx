/**
 * The animatic style's moving parts (styleMode.ts, styleAnimatic.css). Each one
 * renders nothing extra in the default style: ScrambleText and TypedText give
 * back the plain text, and the status lines are not drawn at all.
 *
 * Motion is short and only on arrival (a section opening): the heading resolves
 * from scrambled glyphs in about 0.4 s, the line under it types in about 0.6 s.
 * With `prefers-reduced-motion`, or in capture mode, both are shown finished.
 */
import { useEffect, useState } from "react";
import { MODE } from "../transport";
import { VIEWER_ID } from "../relayEnv";
import { NAV, visibleNav, type NavItem } from "../navConfig";
import { motionAllowed, scrambleFrame, useStyleMode } from "../styleMode";

const SCRAMBLE_MS = 420;
const TYPE_MS_PER_CHAR = 9;
const TYPE_MAX_MS = 650;

/** power2.out: fast, then settling — the animatic's easing. */
const ease = (t: number) => 1 - (1 - t) * (1 - t);

function useAnimaticMotion(): boolean {
  const [style] = useStyleMode();
  return style === "animatic" && motionAllowed();
}

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
  const moving = useAnimaticMotion();
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

/** A line typed out with a block cursor after it; the cursor stays, blinking. */
export function TypedText({ text }: { text: string }) {
  const [style] = useStyleMode();
  const moving = useAnimaticMotion();
  const per = Math.min(TYPE_MS_PER_CHAR, TYPE_MAX_MS / Math.max(1, text.length));
  const total = per * text.length;
  const at = useArrival(text, total, moving);
  if (style !== "animatic") return <>{text}</>;
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

const WHERE = MODE === "hermes" ? "emporia ~ hermes plugin" : MODE === "console" ? "emporia ~ local console" : "emporia ~ dashboard";

/** The status line above a section: where this runs, the section's key, its place among the sections. */
export function AnimaticHudTop({ item }: { item: NavItem }) {
  const [style] = useStyleMode();
  if (style !== "animatic") return null;
  // Read from the viewer, not the relay context, so this module stays free of the relay's
  // state (tests that mock relayEnv in part render PageHeading too). The operator's own
  // section counts only on that section.
  const shown = visibleNav({ isSpectator: VIEWER_ID === null, isOperator: item.id === "relay" });
  const at = shown.findIndex((n) => n.id === item.id);
  const n = (at < 0 ? NAV.findIndex((x) => x.id === item.id) : at) + 1;
  const pad = (x: number) => String(x).padStart(2, "0");
  return (
    <div className="e-anim-hud e-anim-hud--top" aria-hidden>
      <span className="e-anim-hud__where">{WHERE}</span>
      <span className="e-anim-hud__key">{item.hotkey}</span>
      <span className="e-anim-hud__at">{pad(n)} / {pad(shown.length)} · {item.id}</span>
      {/* Keyed by section: it flashes once each time a section opens (the animatic's beat flash). */}
      <span key={item.id} className="e-anim-flash" />
    </div>
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
  const [style] = useStyleMode();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (style !== "animatic") return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [style]);
  if (style !== "animatic") return null;
  return (
    <div className="e-anim-hud e-anim-hud--foot" aria-hidden>
      <span>{clock(now - OPENED)} · {VIEWER_ID ? `as ${VIEWER_ID}` : "spectator"}</span>
      <span>{section} · watch only · s: style</span>
    </div>
  );
}
