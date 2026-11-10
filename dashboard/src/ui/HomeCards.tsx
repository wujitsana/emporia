/**
 * Home as cards: the demo animatic's information cards, one per thing worth a glance. Each card is a
 * status line (what · when), a small chip, a pixel headline of two to five words that decodes on arrival,
 * at most one line of detail, a tint-filled caption bar with the one key line, and a muted footer. A section
 * is one horizontal row of them (scroll-snap, faded edges, keyboard-scrollable). The look is the page's own
 * (dashboard.css § 30: brackets, pixel face, tint fill and ink), so every theme and tint carries over.
 * Cards only display and open the detail; nothing here acts. Fields and helpers: homeCardsModel.ts.
 */
import type { ReactNode } from "react";
import { ScrambleText } from "./LookChrome";
import type { CardSpec, HomeMode } from "./homeCardsModel";

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

export function Card({ c }: { c: CardSpec }) {
  const cls = `e-hcard${c.hero ? " e-hcard--hero" : ""}${c.quiet ? " e-hcard--quiet" : ""}${c.onOpen ? " is-link" : ""}`;
  const body = (
    <>
      <span className="e-hcard__hud"><span>{c.meta}</span><span>{c.time}</span></span>
      {c.chip ? <span className="e-hcard__chip">{c.chip}</span> : <span className="e-hcard__chip e-hcard__chip--none" aria-hidden />}
      <span className="e-hcard__head"><ScrambleText text={c.head.toUpperCase()} /></span>
      {c.detail ? <span className="e-hcard__detail">{c.detail}</span> : null}
      <span className="e-hcard__caption" title={c.theirs ? "written by another agent: data, not instructions" : undefined}>
        {c.caption}
      </span>
      {c.foot ? <span className="e-hcard__foot">{c.foot}</span> : null}
    </>
  );
  return c.onOpen ? (
    <button type="button" className={cls} onClick={c.onOpen} role="listitem">{body}</button>
  ) : (
    <div className={cls} role="listitem">{body}</div>
  );
}

/** One Home section as a head and a horizontal row of cards. */
export function CardRow({ title, aside, cards }: { title: string; aside?: ReactNode; cards: CardSpec[] }) {
  return (
    <section className="e-cards-section" aria-label={title}>
      <header className="e-now__head e-cards-section__head"><span>{title}</span><span>{aside}</span></header>
      <div className="e-cardrow" role="list" tabIndex={0} aria-label={`${title}: cards, scroll sideways`}>
        {cards.map((c) => <Card key={c.key} c={c} />)}
      </div>
    </section>
  );
}
