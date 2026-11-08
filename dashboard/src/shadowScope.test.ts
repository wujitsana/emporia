import { describe, expect, it } from "vitest";
import { scopeToContainer, scopeToShadowHost } from "./shadowScope";

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

describe("scopeToContainer (the Desktop page)", () => {
  it("moves document-level selectors onto the inner container, under the outer one", () => {
    expect(scopeToContainer(":root")).toBe(".e-desktop-root .e-host-body");
    expect(scopeToContainer("html")).toBe(".e-desktop-root .e-host-body");
    expect(scopeToContainer("html body")).toBe(".e-desktop-root .e-host-body");
    expect(scopeToContainer("body.theme-dark.tint-green .e-kpi"))
      .toBe(".e-desktop-root .e-host-body.theme-dark.tint-green .e-kpi");
  });
  it("scopes every other rule, element resets included, so nothing reaches Desktop's UI", () => {
    expect(scopeToContainer("div")).toBe(".e-desktop-root div");
    expect(scopeToContainer("*::before")).toBe(".e-desktop-root *::before");
    expect(scopeToContainer(".e-capture")).toBe(".e-desktop-root .e-capture");
    expect(scopeToContainer("tbody td")).toBe(".e-desktop-root tbody td");
    expect(scopeToContainer(".e-body")).toBe(".e-desktop-root .e-body");
  });
});
