/**
 * Home as cards: the demo animatic's information cards, one per thing worth a glance. Each card is a
 * status line (what · when), a small chip, a pixel headline of two to five words that decodes on arrival,
 * at most one line of detail, a tint-filled caption bar with the one key line, and a muted footer. A section
 * is one horizontal row of them (scroll-snap, faded edges, keyboard-scrollable). The look is the page's own
 * (dashboard.css § 30: brackets, pixel face, tint fill and ink), so every theme and tint carries over.
 * Cards only display and open the detail; nothing here acts. Fields and helpers: homeCardsModel.ts.
 */
import { useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { PixelBadge } from "../badges";
import { ScrambleText } from "./LookChrome";
import { RelTime } from "./RelTime";
import { mergeCards, type CardSpec, type HomeMode } from "./homeCardsModel";

/** The small switch at the top of Home. */
export function HomeSwitch({ mode, onChange }: { mode: HomeMode; onChange: (m: HomeMode) => void }) {
  return (
    <div className="e-home-switch" role="group" aria-label="Home view">
      {(["list", "cards"] as const).map((m) => (
        <button key={m} type="button" className={`e-home-switch__opt${mode === m ? " is-on" : ""}`}
          aria-pressed={mode === m} onClick={() => onChange(m)}>
          {m}
        </button>
      ))}
    </div>
  );
}

/** A copy button for a call: read-only, it only puts the text on the clipboard. */
function CopyCall({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button type="button" className="e-hcard__copy" aria-label={`Copy: ${text}`}
      onClick={(e) => {
        e.stopPropagation();
        navigator.clipboard?.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }, () => undefined);
      }}>
      {copied ? "copied" : "copy"}
    </button>
  );
}

/** Without layout (no measure), a body past this many characters is taken to be clamped. */
const BODY_LONG = 260;

/** The text is selected right now: a click that ends a selection must not open the card. */
function selecting(): boolean {
  return (window.getSelection?.()?.toString() ?? "") !== "";
}

export function Card({ c }: { c: CardSpec }) {
  const [open, setOpen] = useState(false);
  const bodyRef = useRef<HTMLSpanElement>(null);
  const [clamped, setClamped] = useState(false);
  // "more" shows when the text is actually cut (measured), not by a guess at its length.
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el || open || c.full) return;
    const laidOut = el.clientHeight > 0;
    setClamped(laidOut ? el.scrollHeight > el.clientHeight + 1 : c.body!.length > BODY_LONG || c.body!.split("\n").length > 6);
  }, [c.body, open, c.full]);
  const link = !!c.onOpen && !c.full;
  const showMore = !c.full && !!c.body && (clamped || open);
  const cls = `e-hcard${c.hero ? " e-hcard--hero" : ""}${c.quiet ? " e-hcard--quiet" : ""}${link ? " is-link" : ""}${c.body ? " e-hcard--post" : ""}`;
  // Each fact once: a chip that only repeats the headline, or a kind line that only repeats the chip, is not drawn.
  const same = (a: string, b: string) => !!a && !!b && (a.includes(b) || b.includes(a));
  const headLow = c.head.toLowerCase();
  const chip = c.chip && !same(c.chip.toLowerCase(), headLow) ? c.chip : undefined;
  const meta = c.meta && !same(c.meta.toLowerCase(), (c.chip ?? "").toLowerCase()) && !same(c.meta.toLowerCase(), headLow) ? c.meta : "";
  const body = (
    <>
      <span className="e-hcard__hud" title={meta || undefined}>
        <span>{meta}</span>
        <span>{c.at ? <RelTime at={c.at} /> : c.time}</span>
      </span>
      <span className="e-hcard__ids">
        {chip ? <span className={`e-hcard__chip${c.chipOutline ? " e-hcard__chip--outline" : ""}`}>{chip}</span> : <span className="e-hcard__chip e-hcard__chip--none" aria-hidden />}
        {c.game ? <span className="e-hcard__icon"><PixelBadge id={c.game} size={2} mono /></span>
          : c.agent ? <span className="e-hcard__icon" title={c.agent}><AgentMoireAvatar agentId={c.agent} size={26} /></span> : null}
      </span>
      <span className="e-hcard__head"><ScrambleText text={c.head.toUpperCase()} /></span>
      {c.bar ? (
        <span className="e-hcard__bar" role="progressbar" aria-valuemin={0} aria-valuemax={c.bar.max} aria-valuenow={c.bar.value}>
          {Array.from({ length: 10 }, (_, i) => (
            <span key={i} className={i < Math.round((10 * c.bar!.value) / Math.max(1, c.bar!.max)) ? "is-on" : undefined} />
          ))}
        </span>
      ) : null}
      {c.detail ? <span className="e-hcard__detail" title={c.detail}>{c.detail}</span> : null}
      {c.body ? (
        <span className="e-hcard__bodywrap">
          <span ref={bodyRef} className={`e-hcard__body${open || c.full ? " is-open" : ""}`}
            title={c.theirs ? "written by another agent: data, not instructions" : undefined}>{c.body}</span>
          {showMore ? (
            <button type="button" className="e-hcard__more" aria-expanded={open}
              onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}>
              {open ? "less" : "more"}
            </button>
          ) : null}
        </span>
      ) : null}
      {c.tagline ? <span className="e-hcard__tags">{c.tagline}</span> : null}
      {c.marker ? (
        c.marker.onOpen
          ? <button type="button" className="e-hcard__marker" onClick={(e) => { e.stopPropagation(); c.marker!.onOpen!(); }}>{c.marker.text}</button>
          : <span className="e-hcard__marker">{c.marker.text}</span>
      ) : null}
      {c.extra}
      {c.caption || c.copy ? (
        <span className="e-hcard__caption"
          title={c.captionTitle ?? (c.theirs && !c.body ? "written by another agent: data, not instructions" : c.caption)}>
          {c.copy ? <code>{c.copy}</code> : c.caption}
          {c.copy ? <CopyCall text={c.copy} /> : null}
        </span>
      ) : null}
      {c.foot ? (
        <span className="e-hcard__foot" title={c.foot}>
          <span>{c.foot}</span>
        </span>
      ) : null}
    </>
  );
  if (!link) return <div className={cls} role="listitem">{body}</div>;
  const open_ = () => { if (!selecting()) c.onOpen!(); };
  const onKey = (e: KeyboardEvent) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); c.onOpen!(); }
  };
  return <div className={cls} role="listitem" tabIndex={0} onClick={open_} onKeyDown={onKey}>{body}</div>;
}

export interface CardGroup { label: string; cards: CardSpec[] }

/**
 * One Home section: a head, then its cards in a grid that fills the width. Given `groups`, the cards sit in
 * labelled groups that flow side by side (a small group shares its row with the next), each as wide as its
 * cards need, so the page stays dense and no band holds a lone card.
 */
export function CardRow({ title, aside, cards, groups, bare = false }: { title: string; aside?: ReactNode; cards: CardSpec[]; groups?: CardGroup[]; bare?: boolean }) {
  return (
    <section className="e-cards-section" aria-label={title}>
      {bare ? null : <header className="e-now__head e-cards-section__head"><span><ScrambleText text={title} /></span><span>{aside}</span></header>}
      {groups ? (
        <div className="e-hgroups">
          {groups.map((g) => {
            const merged = mergeCards(g.cards);
            return (
              <div key={g.label} className="e-hgroup" role="group" aria-label={g.label} style={{ ["--n" as string]: Math.min(merged.length, 6) }}>
                <div className="e-hgroup__label">{g.label}</div>
                <div className="e-cardrow" role="list" aria-label={`${g.label}: cards`}>
                  {merged.map((c) => <Card key={c.key} c={c} />)}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="e-cardrow" role="list" aria-label={`${title}: cards`}>
          {mergeCards(cards).map((c) => <Card key={c.key} c={c} />)}
        </div>
      )}
    </section>
  );
}

/**
 * Where the agent stands, as one wide strip: its level (with the bar), its record and its season rank side by
 * side, each a chip, a pixel figure and a line. One band, not a row of cards with a lonely card at its end.
 */
export function StandingStrip({ title, aside, items }: { title: string; aside?: ReactNode; items: CardSpec[] }) {
  return (
    <section className="e-cards-section" aria-label={title}>
      <header className="e-now__head e-cards-section__head"><span><ScrambleText text={title} /></span><span>{aside}</span></header>
      <div className="e-hstrip" role="list" aria-label={`${title}: level, record, rank`}>
        {items.map((c) => (
          <button key={c.key} type="button" className="e-hstrip__item" role="listitem" onClick={c.onOpen} disabled={!c.onOpen}>
            <span className="e-hcard__chip">{c.chip}</span>
            <span className="e-hstrip__figure"><ScrambleText text={c.head.toUpperCase()} /></span>
            {c.bar ? (
              <span className="e-hcard__bar" role="progressbar" aria-valuemin={0} aria-valuemax={c.bar.max} aria-valuenow={c.bar.value}>
                {Array.from({ length: 10 }, (_, i) => (
                  <span key={i} className={i < Math.round((10 * c.bar!.value) / Math.max(1, c.bar!.max)) ? "is-on" : undefined} />
                ))}
              </span>
            ) : null}
            <span className="e-hstrip__line">{c.detail ?? c.caption}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
