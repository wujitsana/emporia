// @vitest-environment jsdom
/**
 * The Desktop page's own layer, in `desktop` mode (fixed at build time, so
 * every module is loaded fresh with VITE_EMPORIA_MODE=desktop): its in-memory
 * routing, its profile switching, and what register(ctx) contributes.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DesktopBridge } from "./bridge";

beforeEach(() => {
  vi.stubEnv("VITE_EMPORIA_MODE", "desktop");
  vi.resetModules();
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
});

/** A ctx.rest that serves the console's agent list (or the setup-needed 503). */
function fakeBridge(agents: string[] | "setup"): DesktopBridge & { paths: string[] } {
  const paths: string[] = [];
  const store = new Map<string, unknown>();
  return {
    paths,
    rest: async <T,>(path: string) => {
      paths.push(path);
      if (path === "/agents") {
        if (agents === "setup") {
          const e = new Error('503: {"detail":"not set up","setup_needed":true}');
          throw Object.assign(e, { statusCode: 503 });
        }
        return { agents } as T;
      }
      return {} as T;
    },
    socket: () => () => {},
    storage: {
      get: <T,>(k: string, fallback: T) => (store.has(k) ? (store.get(k) as T) : fallback),
      set: (k: string, v: unknown) => { store.set(k, v); },
      remove: (k: string) => { store.delete(k); },
    },
  };
}

describe("routing on the Desktop page", () => {
  it("keeps the route in memory and never touches the app's own hash router", async () => {
    const route = await import("../dashboardRoute");
    expect(route.routeFromHash().view).toBe("overview");
    const heard = vi.fn();
    const off = route.onRouteChange(heard);
    route.goToHash("#/games/s-1");
    expect(heard).toHaveBeenCalledTimes(1);
    expect(route.routeFromHash()).toEqual({ view: "games", sessionId: "s-1" });
    route.syncHash("season");                      // the app recording where it is: silent
    expect(heard).toHaveBeenCalledTimes(1);
    expect(route.currentHash()).toBe("#/season");
    expect(window.location.hash).toBe("");
    route.resetRoute();
    expect(route.currentHash()).toBe("#/home");
    off();
    route.goToHash("#/agora");
    expect(heard).toHaveBeenCalledTimes(1);
  });
});

describe("the profile on the Desktop page", () => {
  it("views as the profile's agent, and a switch remounts with nothing of the last one", async () => {
    const { setDesktopBridge } = await import("./bridge");
    const { ProfileMount, ScopedRoot } = await import("./mount");
    const env = await import("../relayEnv");
    const { hermesProfile } = await import("../transport");
    const { goToHash, currentHash } = await import("../dashboardRoute");
    setDesktopBridge(fakeBridge(["alpha", "beta"]));

    // What the app shows for a viewer: its id, and a thread only that viewer may read.
    function Probe() {
      return <div data-testid="probe">{`viewer=${env.VIEWER_ID} dm=${env.VIEWER_ID === "alpha" ? "alpha's secret" : "-"}`}</div>;
    }
    const page = (profile: string) => (
      <ScopedRoot><ProfileMount key={profile} profile={profile} render={() => <Probe />} /></ScopedRoot>
    );

    const view = render(page("alpha"));
    expect(await screen.findByText("viewer=alpha dm=alpha's secret")).toBeTruthy();
    expect(hermesProfile()).toEqual({ name: "alpha", isAgent: true });
    expect(document.querySelector(".e-desktop-root > .e-host-body")).toBeTruthy();
    // Desktop's base layer is taken back to browser defaults first; the app's own sheet comes after it.
    const sheet = document.querySelector(".e-desktop-root > style")!.textContent!;
    expect(sheet.indexOf("all: revert")).toBeGreaterThan(-1);
    expect(sheet.indexOf("all: revert")).toBeLessThan(sheet.indexOf("__EMPORIA_DESKTOP_CSS__"));
    goToHash("#/messages");

    view.rerender(page("beta"));
    // Between the switch and the console's answer, nobody: alpha's thread is gone at once.
    expect(screen.queryByText(/alpha's secret/)).toBeNull();
    expect(env.VIEWER_ID).toBeNull();
    expect(currentHash()).toBe("#/home");
    expect(await screen.findByText("viewer=beta dm=-")).toBeTruthy();

    view.rerender(page("gamma"));                 // a profile with no agent here
    await act(async () => {});
    expect(env.VIEWER_ID).toBeNull();
    expect(hermesProfile()).toEqual({ name: "gamma", isAgent: false });
  });

  it("says how to set Emporia up in a profile the plugin is not set up in", async () => {
    const { setDesktopBridge } = await import("./bridge");
    const { ProfileMount } = await import("./mount");
    setDesktopBridge(fakeBridge("setup"));
    render(<ProfileMount profile="default" render={() => <div>app</div>} />);
    expect(await screen.findByText("Emporia is not set up in this profile")).toBeTruthy();
    expect(screen.getByText("hermes emporia setup")).toBeTruthy();
    expect(screen.queryByText("app")).toBeNull();
  });
});

describe("register(ctx)", () => {
  it("adds the page, a sidebar row, palette commands, the status chip and a settings page", async () => {
    const { default: plugin } = await import("./plugin");
    const sdk = await import("@hermes/plugin-sdk") as unknown as { navigated: string[] };
    const { currentHash } = await import("../dashboardRoute");
    const { desktopBridge } = await import("./bridge");
    const registered: { id: string; area: string; data?: unknown }[] = [];
    const settings: { id: string }[] = [];
    const disposers: (() => void)[] = [];
    const b = fakeBridge(["alpha"]);
    plugin.register({
      source: "plugin:emporia",
      register: (c) => { registered.push(c); return () => {}; },
      registerMany: (cs) => { registered.push(...cs); return () => {}; },
      registerSettingsPage: (p) => { settings.push(p); return () => {}; },
      onDispose: (fn) => { disposers.push(fn); },
      rest: b.rest,
      socket: b.socket,
      os: { notify: () => {} },
      storage: b.storage!,
    });
    expect(plugin.id).toBe("emporia");
    expect(plugin.defaultEnabled).toBe(false);
    const byArea = (a: string) => registered.filter((c) => c.area === a);
    expect(byArea("routes").map((c) => c.data)).toEqual([{ path: "/emporia" }]);
    expect(byArea("sidebar.nav").map((c) => (c.data as { path: string }).path)).toEqual(["/emporia"]);
    expect(byArea("statusBar.right")).toHaveLength(1);
    const palette = byArea("palette").map((c) => c.data as { label: string; run: () => void });
    expect(palette.map((p) => p.label)).toContain("Emporia: open games");
    expect(palette.map((p) => p.label)).toContain("Emporia: season");
    expect(settings.map((s) => s.id)).toEqual(["settings"]);
    expect(desktopBridge()).not.toBeNull();

    palette.find((p) => p.label === "Emporia: season")!.run();
    expect(currentHash()).toBe("#/season");
    expect(sdk.navigated.at(-1)).toBe("/emporia");

    disposers.forEach((fn) => fn());
    expect(desktopBridge()).toBeNull();
  }, 60_000);   // the entry imports the whole app
});

describe("the Desktop page's routes", () => {
  // Review pass 2: the turn notification opened `#/games/<id>`, a v1 route (it still works, but every
  // other route the page writes is v2, docs/ui-nav-v2.md).
  it("are all v2 routes, destination first", () => {
    type Fs = { readFileSync(p: string, enc: "utf8"): string };
    const proc = (globalThis as unknown as { process: { cwd(): string; getBuiltinModule(m: string): unknown } }).process;
    const fs = proc.getBuiltinModule("node:fs") as Fs;
    for (const f of ["views.tsx", "routes.ts", "chip.tsx"]) {
      const text = fs.readFileSync(`${proc.cwd()}/src/desktop/${f}`, "utf8").replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");
      const routes = [...text.matchAll(/["`](#\/[a-z]+)/g)].map((m) => m[1]);
      expect(routes.filter((r) => !["#/home", "#/play", "#/market", "#/talk", "#/season", "#/more"].includes(r))).toEqual([]);
    }
  });
});

describe("the status chip", () => {
  it("says the turn, opens the game, and tells only of turns that arrive after it opened", async () => {
    const { setDesktopBridge } = await import("./bridge");
    const { AttentionChip } = await import("./chip");
    const { BLOTTO } = await import("../setPlay");
    const game = (id: string) => ({ session_id: id, module_type: BLOTTO, status: "active", participants: ["alpha", "beta"],
      current_agent: "", created_at: "", state: { phase: "commit", commitments: { beta: "x" } } });
    let sessions = [game("g1")];
    setDesktopBridge({
      rest: async <T,>(path: string) => (path.includes("/inbox") ? { unread_total: 2 }
        : path.includes("/sessions") ? { sessions } : {}) as T,
      socket: () => () => {},
    });
    const opened: string[] = [];
    const news: string[] = [];
    render(<AttentionChip profile="alpha" open={(h) => opened.push(h)} onNewTurn={(s) => news.push(s.session_id)} pollMs={40} />);
    const chip = await screen.findByRole("button", { name: "Emporia: your turn" });
    chip.click();
    expect(opened).toEqual(["#/play/games/g1"]);
    sessions = [game("g1"), game("g2")];
    await screen.findByRole("button", { name: "Emporia: your turn ×2" });
    expect(news).toEqual(["g2"]);
  });
});

describe("the tab's chat and look on the Desktop page", () => {
  it("has no chat drawer, even with a Hermes SDK on the window: Desktop's own chat is the agent's", async () => {
    const w = window as { __EMPORIA_HERMES_SDK__?: unknown; __EMPORIA_HERMES_PROFILE__?: unknown };
    w.__EMPORIA_HERMES_SDK__ = { buildWsUrl: async () => "ws://x", authedFetch: async () => new Response("{}") };
    w.__EMPORIA_HERMES_PROFILE__ = { name: "alpha" };
    try {
      const { chatAvailable } = await import("../chat/chatAccess");
      const { AgentChatDrawer, ChatToggle } = await import("../chat/AgentChat");
      expect(chatAvailable()).toBe(false);
      const { container } = render(<div><ChatToggle /><AgentChatDrawer /></div>);
      expect(container.querySelector("button")).toBeNull();
      expect(container.textContent).toBe("");
    } finally {
      delete w.__EMPORIA_HERMES_SDK__;
      delete w.__EMPORIA_HERMES_PROFILE__;
    }
  });

  it("puts the look on the page's own body, never on Desktop's (there is one style, no `s` key)", async () => {
    const { setDesktopBridge } = await import("./bridge");
    const { ScopedRoot } = await import("./mount");
    const { applyTheme } = await import("../themeConfig");
    setDesktopBridge(fakeBridge(["alpha"]));
    render(<ScopedRoot><div /></ScopedRoot>);
    act(() => applyTheme("theme-light", "tint-green", { fade: false }));
    expect(document.querySelector(".e-host-body")!.classList.contains("e-look")).toBe(true);
    expect(document.body.classList.contains("e-look")).toBe(false);
  });
});
