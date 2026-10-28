// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { ensureViewer } from "./viewerBootstrap";

describe("ensureViewer (P227)", () => {
  beforeEach(() => sessionStorage.clear());

  it("views as the Hermes profile's own agent, not the first alphabetically", async () => {
    await ensureViewer(async () => ["alpha", "beta"], async () => "beta");
    expect(sessionStorage.getItem("emporia_viewer")).toBe("beta");
  });

  it("follows the profile even after an earlier choice in this tab", async () => {
    sessionStorage.setItem("emporia_viewer", "alpha");
    await ensureViewer(async () => ["alpha", "beta"], async () => "beta");
    expect(sessionStorage.getItem("emporia_viewer")).toBe("beta");
  });

  it("keeps the earlier choice in a profile that is no agent", async () => {
    sessionStorage.setItem("emporia_viewer", "beta");
    await ensureViewer(async () => ["alpha", "beta"], async () => "");
    expect(sessionStorage.getItem("emporia_viewer")).toBe("beta");
  });

  it("falls back to the first agent", async () => {
    await ensureViewer(async () => ["beta", "alpha"]);
    expect(sessionStorage.getItem("emporia_viewer")).toBe("alpha");
  });
});
