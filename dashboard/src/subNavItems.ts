import {
  ARENA_LENSES, canSee, dealsAvailable, destinationOf, navById, visibleDestinations, visibleHeaderDestinations,
  type ArenaLens, type View,
} from "./navConfig";
import { useRelayCtx } from "./relayCtx";
import { useRelayHealth } from "./healthCtx";
import type { DashboardView } from "./eventNav";

/** What decides which pages this viewer may reach: who they are, and what the relay can do. */
export function useViewerOpts() {
  // P35-2: one visibility rule (navConfig.visibleNav) for the nav and the keys.
  const { isSpectator, isRelayOperator } = useRelayCtx();
  const dealsOn = dealsAvailable(useRelayHealth());
  return { isSpectator, isOperator: isRelayOperator, dealsOn };
}

/** One tab of the sub-row. */
export interface SubItem {
  key: string;
  label: string;
  title: string;
  active: boolean;
  /** The tab is for something this viewer cannot reach or this relay does not run: drawn dimmed, active, going nowhere. */
  off?: boolean;
  badge?: number;
  go: () => void;
}

type Badges = Partial<Record<DashboardView, number>>;

/**
 * The tabs of the current place's sub-row: its visible pages, or the Arena's four lenses. A page a link
 * names that this viewer may not see is drawn too, dimmed and active, so a deep link to a hidden tab never
 * shows a row with nothing selected. A place with one page (Home, Help) has no tabs, only a line saying
 * what the page is, so the row keeps its height.
 */
export function useSubItems(
  view: View,
  lens: ArenaLens,
  badges: Badges,
  go: { view: (id: View) => void; lens: (l: ArenaLens) => void },
): { items: SubItem[]; note: string | null } {
  const opts = useViewerOpts();
  const health = useRelayHealth();
  const dest = destinationOf(view);
  if (dest.id === "arena") {
    // The four pages of the games, then the season (its own page: quests, ladders and the badge catalogue).
    const seasonOff = !!health?.relay_modules && !health.relay_modules.gamify;
    const season = navById("season");
    const items: SubItem[] = ARENA_LENSES.map((l) => ({
      key: l.id, label: l.label, title: `Arena: ${l.label.toLowerCase()}`, active: view !== "season" && l.id === lens, go: () => go.lens(l.id),
    }));
    if (season) {
      items.push({
        key: season.id, label: season.label, title: seasonOff ? "Off on this relay" : season.blurb, active: view === "season",
        off: seasonOff, go: () => go.view("season"),
      });
    }
    return { items, note: null };
  }
  const visible = [...visibleDestinations(opts), ...visibleHeaderDestinations(opts)].find((d) => d.id === dest.id)?.items ?? [];
  // A module the relay does not run: its tab stays, dimmed, and says so (never a page quoting settings).
  const mods = health?.relay_modules;
  const offModule = (id: string) => !!mods && ((id === "labs" && !mods.labs) || (id === "season" && !mods.gamify));
  const items: SubItem[] = visible.map((n) => {
    const off = offModule(n.id);
    return {
      key: n.id, label: n.label, title: off ? "Off on this relay" : n.blurb, active: n.id === view, off, badge: badges[n.id],
      go: () => go.view(n.id),
    };
  });
  // Deals is drawn only on a relay that runs them (escrow on); a link to it on one that does not still opens
  // the page, as a dimmed active tab (hiddenCurrent below), so nothing is left with no tab selected.
  const hiddenCurrent = !canSee(view, opts) && !items.some((i) => i.key === view) ? navById(view) : undefined;
  if (hiddenCurrent) {
    items.push({ key: hiddenCurrent.id, label: hiddenCurrent.label, title: hiddenCurrent.blurb, active: true, off: true, go: () => {} });
  }
  // Home needs no words in the row (the page says it); the row keeps its height all the same.
  return { items, note: items.length < 2 ? (dest.id === "home" || dest.id === "messages" ? "" : dest.blurb) : null };
}
