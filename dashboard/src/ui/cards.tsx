import type { ReactNode } from "react";
import { ScrambleText } from "./LookChrome";

/** Minimal retro panel — thin border, small label, no SRCL Card chrome. */
export function SlimCard({
  title,
  children,
  action,
  foot,
}: {
  title?: string;
  children: ReactNode;
  action?: ReactNode;
  foot?: ReactNode;
}) {
  return (
    <section className="e-slim-card">
      {(title || action) && (
        <header className="e-slim-card__head">
          {title ? <span className="e-slim-card__title"><ScrambleText text={title} /></span> : <span />}
          {action}
        </header>
      )}
      <div className="e-slim-card__body">{children}</div>
      {foot ? <footer className="e-slim-card__foot">{foot}</footer> : null}
    </section>
  );
}

export function MetaGrid({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="e-meta-grid">
      {rows.map(([k, v]) => (
        <div key={k} className="e-meta-grid__row">
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}
