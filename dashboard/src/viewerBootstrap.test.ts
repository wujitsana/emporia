// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { ensureViewer } from "./viewerBootstrap";

describe("ensureViewer (P227)", () => {
  beforeEach(() => sessionStorage.clear());

  it("views as the Hermes profile's own agent, not the first alphabetically", async () => {
    expect(await ensureViewer(async () => ["alpha", "beta"], async () => "beta"))
      .toEqual({ name: "beta", isAgent: true });
    expect(sessionStorage.getItem("emporia_viewer")).toBe("beta");
  });

  it("follows a new profile even after an earlier choice in this tab", async () => {
    sessionStorage.setItem("emporia_hermes_profile", "alpha");
    sessionStorage.setItem("emporia_viewer", "alpha");
    await ensureViewer(async () => ["alpha", "beta"], async () => "beta");
    expect(sessionStorage.getItem("emporia_viewer")).toBe("beta");
  });

  it("keeps a switcher choice while the profile stays the same", async () => {
    await ensureViewer(async () => ["alpha", "beta"], async () => "beta");
    sessionStorage.setItem("emporia_viewer", "alpha");          // picked in the switcher; tab reloads
    await ensureViewer(async () => ["alpha", "beta"], async () => "beta");
    expect(sessionStorage.getItem("emporia_viewer")).toBe("alpha");
  });

  it("keeps the earlier choice outside the Hermes dashboard", async () => {
    sessionStorage.setItem("emporia_viewer", "beta");
    expect(await ensureViewer(async () => ["alpha", "beta"], async () => "")).toBeNull();
    expect(sessionStorage.getItem("emporia_viewer")).toBe("beta");
  });

  it("views as nobody in a Hermes profile that is no agent (P231)", async () => {
    sessionStorage.setItem("emporia_viewer", "alpha");
    expect(await ensureViewer(async () => ["alpha", "beta"], async () => "coder"))
      .toEqual({ name: "coder", isAgent: false });
    expect(sessionStorage.getItem("emporia_viewer")).toBeNull();
  });

  it("falls back to the first agent", async () => {
    await ensureViewer(async () => ["beta", "alpha"]);
    expect(sessionStorage.getItem("emporia_viewer")).toBe("alpha");
  });
});
