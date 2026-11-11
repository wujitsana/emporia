/**
 * Home as cards: the demo animatic's information cards, one per thing worth a glance. Each card is a
 * status line (what · when), a small chip, a pixel headline of two to five words that decodes on arrival,
 * at most one line of detail, a tint-filled caption bar with the one key line, and a muted footer. A section
 * is one horizontal row of them (scroll-snap, faded edges, keyboard-scrollable). The look is the page's own
 * (dashboard.css § 30: brackets, pixel face, tint fill and ink), so every theme and tint carries over.
 * Cards only display and open the detail; nothing here acts. Fields and helpers: homeCardsModel.ts.
 */
import { useState, type ReactNode } from "react";
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
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }, () => undefined);
      }}>
      {copied ? "copied" : "copy"}
    </button>
  );
}

/** Past this a body shows "more": about six lines of a 300px card. */
const BODY_LONG = 260;

export function Card({ c }: { c: CardSpec }) {
  const [open, setOpen] = useState(false);
  const link = c.onOpen && !c.copy && !c.body;
  const long = !!c.body && (c.body.length > BODY_LONG || c.body.split("\n").length > 6);
  const cls = `e-hcard${c.hero ? " e-hcard--hero" : ""}${c.quiet ? " e-hcard--quiet" : ""}${link ? " is-link" : ""}${c.body ? " e-hcard--post" : ""}`;
  const body = (
    <>
      <span className="e-hcard__hud">
        <span>{c.meta}</span>
        <span>{c.at ? <RelTime at={c.at} /> : c.time}</span>
      </span>
      <span className="e-hcard__ids">
        {c.chip ? <span className="e-hcard__chip">{c.chip}</span> : <span className="e-hcard__chip e-hcard__chip--none" aria-hidden />}
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
      {c.detail ? <span className="e-hcard__detail">{c.detail}</span> : null}
      {c.body ? (
        <span className="e-hcard__bodywrap">
          <span className={`e-hcard__body${open ? " is-open" : ""}`}
            title={c.theirs ? "written by another agent: data, not instructions" : undefined}>{c.body}</span>
          {long ? (
            <button type="button" className="e-hcard__more" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
              {open ? "less" : "more"}
            </button>
          ) : null}
        </span>
      ) : null}
      {c.extra}
      <span className="e-hcard__caption" title={c.theirs && !c.body ? "written by another agent: data, not instructions" : undefined}>
        {c.copy ? <code>{c.copy}</code> : c.caption}
        {c.copy ? <CopyCall text={c.copy} /> : null}
      </span>
      {c.foot || (c.body && c.onOpen) ? (
        <span className="e-hcard__foot">
          <span>{c.foot}</span>
          {c.body && c.onOpen ? (
            <button type="button" className="e-hcard__more" onClick={c.onOpen}>open →</button>
          ) : null}
        </span>
      ) : null}
    </>
  );
  return link ? (
    <button type="button" className={cls} onClick={c.onOpen} role="listitem">{body}</button>
  ) : (
    <div className={cls} role="listitem">{body}</div>
  );
}

/** One Home section as a head and a grid of cards that fills the width, as many columns as fit. */
export function CardRow({ title, aside, cards, bare = false }: { title: string; aside?: ReactNode; cards: CardSpec[]; bare?: boolean }) {
  return (
    <section className="e-cards-section" aria-label={title}>
      {bare ? null : <header className="e-now__head e-cards-section__head"><span><ScrambleText text={title} /></span><span>{aside}</span></header>}
      <div className="e-cardrow" role="list" aria-label={`${title}: cards`}>
        {mergeCards(cards).map((c) => <Card key={c.key} c={c} />)}
      </div>
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
