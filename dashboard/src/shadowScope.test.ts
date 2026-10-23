import { describe, expect, it } from "vitest";
import { scopeToShadowHost } from "../vite.plugin.config";

describe("scopeToShadowHost (P126)", () => {
  it("moves document-level selectors onto the shadow host", () => {
    expect(scopeToShadowHost(":root")).toBe(":host");
    expect(scopeToShadowHost("html")).toBe(":host");
    expect(scopeToShadowHost("body.theme-dark")).toBe(".e-host-body.theme-dark");
    expect(scopeToShadowHost("body.theme-dark .e-kpi")).toBe(".e-host-body.theme-dark .e-kpi");
    expect(scopeToShadowHost("html body")).toBe(":host .e-host-body");
  });
  it("leaves everything else alone", () => {
    expect(scopeToShadowHost(".tbody-row")).toBe(".tbody-row");
    expect(scopeToShadowHost(".e-body")).toBe(".e-body");
    expect(scopeToShadowHost("tbody td")).toBe("tbody td");
  });
});
