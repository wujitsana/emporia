import Text from "@components/Text";
import { NAV, type View } from "./navConfig";
import { useRelayCtx } from "./relayContext";
import { ThemePanelBody } from "./ThemeControls";
import type { Theme, Tint } from "./themeConfig";
import type { DashboardView } from "./eventNav";

function useVisibleNav() {
  const { isRelayOperator, isSpectator } = useRelayCtx();
  return NAV.filter((n) => {
    if (n.id === "fees") return !isSpectator;
    if (n.id === "messages") return !isSpectator;
    return true;
  });
}

/** Single nav source: horizontal tabs (desktop header) or drawer list (mobile). */
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
  const visibleNav = useVisibleNav();
  const shellView = view === "fees" ? "overview" : view;
  return (
    <nav
      className={`e-section-tabs e-section-tabs--${variant}`}
      aria-label="Sections"
    >
      {visibleNav.map((n) => {
        const active = shellView === n.id;
        const badgeCount = badges[n.id] ?? 0;
        return (
          <button
            key={n.id}
            type="button"
            className={`e-section-tab${active ? " is-active" : ""}`}
            onClick={() => onGo(n.id)}
            title={n.blurb}
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
    </nav>
  );
}

/** Mobile slide-out — same sections as header tabs + theme. */
export function MobileNavDrawer({
  view,
  onGo,
  theme,
  tint,
  onTheme,
  onTint,
  badges,
}: {
  view: View;
  onGo: (id: View) => void;
  theme: Theme;
  tint: Tint;
  onTheme: (t: Theme) => void;
  onTint: (t: Tint) => void;
  badges?: Partial<Record<DashboardView, number>>;
}) {
  return (
    <div className="e-mobile-drawer">
      <Text className="e-brand-title">✶ Emporia</Text>
      <SectionTabs view={view} onGo={onGo} variant="drawer" badges={badges} />
      <div className="e-mobile-drawer__theme">
        <ThemePanelBody theme={theme} tint={tint} onTheme={onTheme} onTint={onTint} />
      </div>
    </div>
  );
}
