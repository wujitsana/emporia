import Badge from "@kit/Badge";
import Text from "@kit/Text";
import { Component, type ReactNode } from "react";
import type { NavItem } from "../navConfig";

/**
 * The section's own heading: its name, and one line on what it shows. Every
 * section opens with it, so a first-time viewer always knows where they are —
 * on a phone the tabs are folded into the menu and this is the only label.
 */
export function PageHeading({ item }: { item: NavItem | undefined }) {
  if (!item) return null;
  return (
    <header className="e-page-head">
      <h1 className="e-page-head__title">
        <span className="e-page-head__icon" aria-hidden>{item.icon}</span>
        {item.label}
      </h1>
      <p className="e-page-head__blurb">{item.blurb}</p>
    </header>
  );
}

/**
 * One section failing to render must not blank the whole dashboard (header and
 * nav included) until a reload. The boundary names the section and keeps the
 * nav working; App keys it by section, so moving to another section resets it.
 */
export class ViewBoundary extends Component<{ name: string; children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="e-view-body">
        <div className="e-empty-pane" role="alert">
          <div className="e-empty-pane__title">{this.props.name} could not be shown.</div>
          <div className="e-empty-pane__hint">
            Something in what the relay sent did not fit this view ({this.state.error.message || "unknown error"}).
            The other sections still work — pick one from the menu, or reload to try again.
          </div>
        </div>
      </div>
    );
  }
}

/** Page content shell — the section's name and blurb come from PageHeading above it. */
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