import Badge from "@components/Badge";
import Text from "@components/Text";
import type { ReactNode } from "react";

/** Page content shell — title lives in app top bar, not duplicated here. */
export function ViewBody({
  children,
  toolbar,
  flush,
}: {
  children: ReactNode;
  toolbar?: ReactNode;
  flush?: boolean;
}) {
  return (
    <div className={`e-view-body${flush ? " e-view-body--flush" : ""}`}>
      {toolbar ? <div className="e-view-toolbar">{toolbar}</div> : null}
      <div className="e-view-body__inner">{children}</div>
    </div>
  );
}

export function PageToolbar({ children }: { children: ReactNode }) {
  return <div className="e-page-toolbar">{children}</div>;
}

export function StatHero({
  items,
}: {
  items: { label: string; value: string; hint?: string; accent?: boolean }[];
}) {
  return (
    <div className="e-hero-stats" role="list">
      {items.map((it) => (
        <div
          key={it.label}
          className={`e-hero-stat${it.accent ? " e-hero-stat--accent" : ""}`}
          role="listitem"
        >
          <span className="e-hero-stat__value">{it.value}</span>
          <span className="e-hero-stat__label">{it.label}</span>
          {it.hint ? <span className="e-hero-stat__hint">{it.hint}</span> : null}
        </div>
      ))}
    </div>
  );
}

export function PanelCard({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="e-panel-card">
      <header className="e-panel-card__head">
        <Text className="e-panel-card__title">{title}</Text>
        {action}
      </header>
      <div className="e-panel-card__body">{children}</div>
    </section>
  );
}

export function FeedEventType({ type }: { type: string }) {
  const label = type.replace(/_/g, " ");
  return <span className="e-feed-type">{label}</span>;
}

export function NavMetricBadge({ children }: { children: ReactNode }) {
  return <Badge className="e-nav-metric">{children}</Badge>;
}