import { describe, expect, it } from "vitest";
import { themeFromParam, tintFromParam } from "./capture";

describe("capture URL parameters", () => {
  it("reads a tint by its plain name, amber and default read as green", () => {
    expect(tintFromParam("green")).toBe("tint-green");
    expect(tintFromParam("tint-pink")).toBe("tint-pink");
    expect(tintFromParam("Purple")).toBe("tint-purple");
    expect(tintFromParam("amber")).toBe("tint-green");
    expect(tintFromParam("default")).toBe("tint-green");
    expect(tintFromParam("orange")).toBe("tint-orange");
    expect(tintFromParam("teal")).toBeNull();
    expect(tintFromParam("")).toBeNull();
    expect(tintFromParam(null)).toBeNull();
  });

  it("reads a theme", () => {
    expect(themeFromParam("light")).toBe("theme-light");
    expect(themeFromParam("dark")).toBe("theme-dark");
    expect(themeFromParam("vivid")).toBe("theme-vivid");
    expect(themeFromParam("sepia")).toBeNull();
  });
});
