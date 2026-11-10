import type { ReactNode } from "react";
import {
  destinationOf, visibleDestinations, visibleHeaderDestinations,
  type ArenaLens, type Destination, type View,
} from "./navConfig";
import { arrowKeys } from "./arrowKeys";
import { useSubItems, useViewerOpts, type SubItem } from "./subNavItems";
import { ThemePanelBody } from "./ThemeControls";
import { ViewerSwitcher } from "./ui/ViewerSwitcher";
import { BrandTitle } from "./ui/LookChrome";
import type { Theme, Tint } from "./themeConfig";
import type { DashboardView } from "./eventNav";

type Badges = Partial<Record<DashboardView, number>>;

function Count({ n }: { n: number }) {
  return n > 0 ? <span className="e-nav-badge" aria-label={`${n} unread`}>{n > 99 ? "99+" : n}</span> : null;
}

/** A destination's badge: the sum of its sections' (the Agent carries Messages' unread). */
function destBadge(views: DashboardView[], badges: Badges): number {
  return views.reduce((sum, v) => sum + (badges[v] ?? 0), 0);
}

/**
 * The five destinations (navConfig.ts): a row of tabs in the desktop header, or the menu's list on a
 * phone. A destination opens its last section, else its first.
 */
export function SectionTabs({
  view,
  onGoDest,
  variant = "horizontal",
  badges = {},
}: {
  view: View;
  onGoDest: (id: Destination) => void;
  variant?: "horizontal" | "drawer";
  badges?: Badges;
}) {
  const dests = visibleDestinations(useViewerOpts());
  const here = destinationOf(view).id;
  return (
    <nav className={`e-section-tabs e-section-tabs--${variant}`} aria-label="Places" onKeyDown={arrowKeys}>
      {dests.map((d) => {
        const active = d.id === here;
        return (
          <button
            key={d.id}
            type="button"
            className={`e-section-tab${active ? " is-active" : ""}`}
            onClick={() => onGoDest(d.id)}
            title={d.blurb}
            aria-current={active ? "page" : undefined}
          >
            <span className="e-section-tab__icon" aria-hidden>{d.icon}</span>
            <span className="e-section-tab__label">{d.label}</span>
            <Count n={destBadge(d.views, badges)} />
            {variant === "drawer" ? (
              <>
                <span className="e-section-tab__sub">{d.items.map((n) => n.label).join(" · ")}</span>
                <kbd className="e-section-tab__key" title={`key ${d.hotkey}`}>{d.hotkey}</kbd>
              </>
            ) : null}
          </button>
        );
      })}
    </nav>
  );
}

/**
 * The header's own places: Help (the docs, key `?`) and, for the relay's operator alone, the relay desk
 * (Relay and Operator). Neither is a destination in the row of tabs.
 */
export function HeaderLinks({ view, onGo, drawer = false }: { view: View; onGo: (id: View) => void; drawer?: boolean }) {
  const places = visibleHeaderDestinations(useViewerOpts());
  const here = destinationOf(view).id;
  return (
    <div className={drawer ? "e-header-links e-header-links--drawer" : "e-header-links"}>
      {places.map((d) => (
        <button
          key={d.id}
          type="button"
          className={`e-header-link${d.id === here ? " is-active" : ""}`}
          onClick={() => onGo(d.items[0].id)}
          title={d.blurb}
          aria-current={d.id === here ? "page" : undefined}
        >
          <span className="e-header-link__icon" aria-hidden>{d.icon}</span>
          <span className="e-header-link__word">{d.label}</span>
        </button>
      ))}
    </div>
  );
}

/**
 * The sub-row: one strip, 40 px, directly under the header on every destination and width. It starts at
 * the same x everywhere (no destination label in front of the tabs; the page title names the place).
 */
export function SubNav({ label, items, note }: { label: string; items: SubItem[]; note: string | null }) {
  return (
    <nav className="e-subnav" aria-label={`${label}: sections`} onKeyDown={arrowKeys}>
      {note ? <span className="e-subnav__note">{note}</span> : items.map((it) => (
        <button
          key={it.key}
          type="button"
          className={`e-subnav__tab${it.active ? " is-active" : ""}${it.off ? " is-off" : ""}`}
          onClick={it.go}
          title={it.title}
          aria-current={it.active ? "page" : undefined}
          aria-disabled={it.off || undefined}
        >
          {it.label}
          <Count n={it.badge ?? 0} />
        </button>
      ))}
    </nav>
  );
}

/** The sub-row for the current place: it reads the viewer and the relay itself, so App just drops it in. */
export function SubRow({
  view, lens, badges = {}, onGoView, onGoLens,
}: {
  view: View;
  lens: ArenaLens;
  badges?: Badges;
  onGoView: (id: View) => void;
  onGoLens: (l: ArenaLens) => void;
}) {
  const { items, note } = useSubItems(view, lens, badges, { view: onGoView, lens: onGoLens });
  return <SubNav label={destinationOf(view).label} items={items} note={note} />;
}

/** Phone: the five places in a bar at the foot of the screen, a dot where something is new. */
export function BottomBar({
  view,
  onGoDest,
  badges = {},
}: {
  view: View;
  onGoDest: (id: Destination) => void;
  badges?: Badges;
}) {
  const dests = visibleDestinations(useViewerOpts());
  const here = destinationOf(view).id;
  return (
    <nav className="e-bottom-bar" aria-label="Places" onKeyDown={arrowKeys}>
      {dests.map((d) => {
        const active = d.id === here;
        // The Labs & Season place is "Season" down here: that is the word a phone has room for.
        const label = d.id === "labs" ? "Season" : d.label;
        return (
          <button
            key={d.id}
            type="button"
            className={`e-bottom-bar__tab${active ? " is-active" : ""}`}
            onClick={() => onGoDest(d.id)}
            aria-current={active ? "page" : undefined}
            aria-label={label}
          >
            <span className="e-bottom-bar__icon" aria-hidden>{d.icon}</span>
            <span className="e-bottom-bar__label">{label}</span>
            <Count n={destBadge(d.views, badges)} />
          </button>
        );
      })}
    </nav>
  );
}

/** The phone's menu: the agent chat, the places, help and the relay desk, then who you view as and the theme. */
export function MobileNavDrawer({
  view,
  onGoDest,
  onGoView,
  onClose,
  theme,
  tint,
  onTheme,
  onTint,
  badges,
  lead,
}: {
  view: View;
  onGoDest: (id: Destination) => void;
  onGoView: (id: View) => void;
  onClose?: () => void;
  /** Drawn above the destinations (the agent chat, in the Hermes tab). */
  lead?: ReactNode;
  theme: Theme;
  tint: Tint;
  onTheme: (t: Theme) => void;
  onTint: (t: Tint) => void;
  badges?: Badges;
}) {
  return (
    <div className="e-mobile-drawer">
      <div className="e-mobile-drawer__head">
        <span className="e-brand-title"><BrandTitle /></span>
        {onClose ? (
          <button type="button" className="e-icon-button" onClick={onClose} aria-label="Close menu">
            ✕
          </button>
        ) : null}
      </div>
      {lead}
      <SectionTabs view={view} onGoDest={onGoDest} variant="drawer" badges={badges} />
      <HeaderLinks view={view} onGo={onGoView} drawer />
      <div className="e-mobile-drawer__foot">
        <ViewerSwitcher />
        <ThemePanelBody theme={theme} tint={tint} onTheme={onTheme} onTint={onTint} />
      </div>
    </div>
  );
}
