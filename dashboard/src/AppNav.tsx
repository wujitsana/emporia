import type { ReactNode } from "react";
import { destinationOf, visibleDestinations, type Destination, type View } from "./navConfig";
import { useRelayCtx } from "./relayCtx";
import { ThemePanelBody } from "./ThemeControls";
import { ViewerSwitcher } from "./ui/ViewerSwitcher";
import { BrandTitle } from "./ui/LookChrome";
import type { Theme, Tint } from "./themeConfig";
import type { DashboardView } from "./eventNav";

type Badges = Partial<Record<DashboardView, number>>;

function useDestinations() {
  // P35-2: one visibility rule (navConfig.visibleNav) for the nav and the keys.
  const { isSpectator, isRelayOperator } = useRelayCtx();
  return visibleDestinations({ isSpectator, isOperator: isRelayOperator });
}

function Count({ n }: { n: number }) {
  return n > 0 ? <span className="e-nav-badge">{n > 99 ? "99+" : n}</span> : null;
}

/** A destination's badge: the sum of its sections' (Talk carries Messages' unread). */
function destBadge(views: DashboardView[], badges: Badges): number {
  return views.reduce((sum, v) => sum + (badges[v] ?? 0), 0);
}

/**
 * The six destinations (navigation v2, docs/ui-nav-v2.md): a row of tabs in the
 * desktop header, or the menu's list on a phone. A destination opens its last
 * section, else its first.
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
  const dests = useDestinations();
  const here = destinationOf(view).id;
  return (
    <nav className={`e-section-tabs e-section-tabs--${variant}`} aria-label="Sections">
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
            <span className="e-section-tab__label">
              {d.label}
              <Count n={destBadge(d.views, badges)} />
            </span>
            {variant === "drawer" ? (
              <>
                <span className="e-section-tab__sub">{d.items.map((n) => n.label).join(" · ")}</span>
                <kbd className="e-section-tab__key">{d.hotkey}</kbd>
              </>
            ) : null}
          </button>
        );
      })}
    </nav>
  );
}

/**
 * The sections inside the current destination — `Games · Deals`, `Agora ·
 * Rooms · Messages` — one strip under the header, on every width. Not drawn for a
 * destination with a single section (Home, Season).
 */
export function SubNav({ view, onGo, badges = {} }: { view: View; onGo: (id: View) => void; badges?: Badges }) {
  const dests = useDestinations();
  const here = destinationOf(view).id;
  const dest = dests.find((d) => d.id === here);
  if (!dest || dest.items.length < 2) return null;
  return (
    <nav className="e-subnav" aria-label={`${dest.label}: sections`}>
      <span className="e-subnav__dest" aria-hidden>{dest.label}</span>
      {dest.items.map((n) => {
        const active = n.id === view;
        return (
          <button
            key={n.id}
            type="button"
            className={`e-subnav__tab${active ? " is-active" : ""}`}
            onClick={() => onGo(n.id)}
            title={n.blurb}
            aria-current={active ? "page" : undefined}
          >
            {n.label}
            <Count n={badges[n.id] ?? 0} />
          </button>
        );
      })}
      <span className="e-subnav__keys" aria-hidden>[ ]</span>
    </nav>
  );
}

/** The phone's menu: the agent chat, the six destinations, then who you view as and the theme. */
export function MobileNavDrawer({
  view,
  onGoDest,
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
      <div className="e-mobile-drawer__foot">
        <ViewerSwitcher />
        <ThemePanelBody theme={theme} tint={tint} onTheme={onTheme} onTint={onTint} />
      </div>
    </div>
  );
}
