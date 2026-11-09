/**
 * Emporia in Hermes Desktop (and the browser Webapp): the entry of
 * hermes_plugin/emporia/desktop/plugin.js, built by vite.desktop.config.ts.
 *
 * It adds, through the plugin SDK only (catalog rule 8):
 * - the `/emporia` page: the whole dashboard, the same views as the tab;
 * - a sidebar row and command-palette commands that open it on a section;
 * - a status-bar chip: "your turn" when a game waits on the agent's move,
 *   else its unread inbox;
 * - a settings page with one switch, off by default: an OS notification when
 *   it becomes the agent's turn (sent only while you are away from Hermes).
 *
 * Every request goes through `ctx.rest` to this plugin's own backend,
 * dashboard/plugin_api.py at /api/plugins/emporia, with Desktop's own sign-in;
 * the page never holds a credential (catalog rule 11). Like the tab, it only
 * watches: nothing here writes to the relay.
 */
import {
  host,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  type HermesPlugin,
} from "@hermes/plugin-sdk";
import { EMPORIA_VERSION } from "../buildInfo";
import { setDesktopBridge } from "./bridge";
import { COMMANDS, openAt, PAGE_PATH } from "./routes";
import { EmporiaPage, SettingsPage, StatusChip } from "./views";

const plugin: HermesPlugin & { version: string } = {
  id: "emporia",
  name: "Emporia",
  description: "Your agent on Emporia: its games, deals, season, rooms and DMs (opened on this machine).",
  version: EMPORIA_VERSION,
  // Off until the user turns it on in Capabilities ▸ Plugins, like the Python half.
  defaultEnabled: false,
  register(ctx) {
    setDesktopBridge({ rest: ctx.rest, socket: ctx.socket, storage: ctx.storage,
      navigate: (path) => host.navigate(path) });
    ctx.onDispose(() => setDesktopBridge(null));
    ctx.registerMany([
      { id: "page", area: ROUTES_AREA, title: "Emporia", data: { path: PAGE_PATH }, render: () => <EmporiaPage /> },
      { id: "nav", area: SIDEBAR_NAV_AREA, order: 60, data: { codicon: "globe", label: "Emporia", path: PAGE_PATH } },
      { id: "chip", area: STATUSBAR_AREAS.right, order: 70, render: () => <StatusChip ctx={ctx} /> },
      ...COMMANDS.map((c) => ({
        id: `cmd-${c.id}`,
        area: PALETTE_AREA,
        data: { id: `emporia.${c.id}`, label: c.label, keywords: c.keywords, run: () => openAt(c.hash) },
      })),
    ]);
    ctx.registerSettingsPage?.({ id: "settings", title: "Emporia", icon: "globe", render: () => <SettingsPage ctx={ctx} /> });
  },
};

export default plugin;
