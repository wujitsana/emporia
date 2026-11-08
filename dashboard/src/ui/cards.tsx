import type { ReactNode } from "react";

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
          {title ? <span className="e-slim-card__title">{title}</span> : <span />}
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

export function RailChips({ rails }: { rails: string[] }) {
  return (
    <div className="e-rail-chips">
      {rails.map((r) => (
        <span key={r} className="e-rail-chip">
          {r.replace(/_/g, " ")}
        </span>
      ))}
    </div>
  );
}