import { describe, expect, it } from "vitest";
import { themeFromParam, tintFromParam } from "./capture";

describe("capture URL parameters", () => {
  it("reads a tint by its plain name, amber as the default tint", () => {
    expect(tintFromParam("green")).toBe("tint-green");
    expect(tintFromParam("tint-pink")).toBe("tint-pink");
    expect(tintFromParam("Purple")).toBe("tint-purple");
    expect(tintFromParam("amber")).toBe("");
    expect(tintFromParam("teal")).toBeNull();
    expect(tintFromParam("")).toBeNull();
    expect(tintFromParam(null)).toBeNull();
  });

  it("reads a theme", () => {
    expect(themeFromParam("light")).toBe("theme-light");
    expect(themeFromParam("dark")).toBe("theme-dark");
    expect(themeFromParam("sepia")).toBeNull();
  });
});
