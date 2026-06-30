import Text from "@components/Text";
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

/** Empty detail pane — add context instead of a single faint line. */
export function EmptyPane({
  title = "Nothing selected",
  hint,
  facts,
}: {
  title?: string;
  hint?: string;
  facts?: [string, string][];
}) {
  return (
    <div className="e-empty-pane">
      <Text className="e-empty-pane__title">{title}</Text>
      {hint ? <Text className="e-empty-pane__hint">{hint}</Text> : null}
      {facts && facts.length > 0 ? (
        <dl className="e-meta-grid e-meta-grid--inline">
          {facts.map(([k, v]) => (
            <div key={k} className="e-meta-grid__row">
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
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