import Text from "@kit/Text";
import { groupedNav, visibleNav, type View } from "./navConfig";
import { useRelayCtx } from "./relayCtx";
import { ThemePanelBody } from "./ThemeControls";
import { ViewerSwitcher } from "./ui/ViewerSwitcher";
import type { Theme, Tint } from "./themeConfig";
import type { DashboardView } from "./eventNav";

function useVisibleNav() {
  // P34-2: `isRelayOperator` was destructured here and never used — the nav
  // gates on `isSpectator` alone, and App.tsx passes operator-ness to FeesView
  // itself. P35-2: the filter itself moved to navConfig.visibleNav(), so the
  // keyboard handler in App.tsx can ask the same question and get the same
  // answer instead of searching the unfiltered list.
  const { isSpectator, isRelayOperator } = useRelayCtx();
  return visibleNav({ isSpectator, isOperator: isRelayOperator });
}

/**
 * Single nav source: horizontal tabs (desktop header) or the menu's list
 * (mobile). Both are drawn in NAV_GROUPS order — the tabs with a quiet rule
 * between groups, the menu with each group's name above it.
 */
export function SectionTabs({
  view,
  onGo,
  variant = "horizontal",
  badges = {},
}: {
  view: View;
  onGo: (id: View) => void;
  variant?: "horizontal" | "drawer";
  badges?: Partial<Record<DashboardView, number>>;
}) {
  const groups = groupedNav(useVisibleNav());
  const tabView = view;
  return (
    <nav
      className={`e-section-tabs e-section-tabs--${variant}`}
      aria-label="Sections"
    >
      {groups.map((g, gi) => (
        <div key={g.title} className="e-section-group" role="group" aria-label={g.title}>
          {variant === "drawer" ? (
            <span className="e-section-group__title" aria-hidden>{g.title}</span>
          ) : gi > 0 ? (
            <span className="e-section-group__rule" aria-hidden />
          ) : null}
          {g.items.map((n) => {
            const active = tabView === n.id;
            const badgeCount = badges[n.id] ?? 0;
            return (
              <button
                key={n.id}
                type="button"
                className={`e-section-tab${active ? " is-active" : ""}`}
                onClick={() => onGo(n.id)}
                title={n.blurb}
                aria-current={active ? "page" : undefined}
              >
                <span className="e-section-tab__icon" aria-hidden>
                  {n.icon}
                </span>
                <span className="e-section-tab__label">
                  {n.label}
                  {badgeCount > 0 && (
                    <span className="e-nav-badge">{badgeCount > 99 ? "99+" : badgeCount}</span>
                  )}
                </span>
                {variant === "drawer" ? (
                  <kbd className="e-section-tab__key">{n.hotkey}</kbd>
                ) : null}
              </button>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

/** Mobile slide-out menu — the same sections as the header tabs, then who you view as and the theme. */
export function MobileNavDrawer({
  view,
  onGo,
  onClose,
  theme,
  tint,
  onTheme,
  onTint,
  badges,
}: {
  view: View;
  onGo: (id: View) => void;
  onClose?: () => void;
  theme: Theme;
  tint: Tint;
  onTheme: (t: Theme) => void;
  onTint: (t: Tint) => void;
  badges?: Partial<Record<DashboardView, number>>;
}) {
  return (
    <div className="e-mobile-drawer">
      <div className="e-mobile-drawer__head">
        <Text className="e-brand-title">✶ Emporia</Text>
        {onClose ? (
          <button type="button" className="e-icon-button" onClick={onClose} aria-label="Close menu">
            ✕
          </button>
        ) : null}
      </div>
      <SectionTabs view={view} onGo={onGo} variant="drawer" badges={badges} />
      <div className="e-mobile-drawer__foot">
        <ViewerSwitcher />
        <ThemePanelBody theme={theme} tint={tint} onTheme={onTheme} onTint={onTint} />
      </div>
    </div>
  );
}
