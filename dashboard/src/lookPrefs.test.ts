import { describe, expect, it } from "vitest";
import { DEFAULT_FONT, DEFAULT_INK, nextInk, normalizeFont, normalizeInk } from "./lookPrefs";

describe("look prefs", () => {
  it("unknown saved values load as the defaults (Pixel, Auto)", () => {
    expect(normalizeFont("comic")).toBe(DEFAULT_FONT);
    expect(normalizeFont(null)).toBe("pixel");
    expect(normalizeInk("pink")).toBe(DEFAULT_INK);
    expect(normalizeFont("serif")).toBe("serif");
    expect(normalizeInk("black")).toBe("black");
  });
  it("the ink cycles Auto, White, Black", () => {
    expect(nextInk("auto")).toBe("white");
    expect(nextInk("white")).toBe("black");
    expect(nextInk("black")).toBe("auto");
  });
});
