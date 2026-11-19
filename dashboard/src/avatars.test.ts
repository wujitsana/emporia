import { sha256 } from "@noble/hashes/sha2.js";
import { describe, expect, it } from "vitest";
import { TINT, tokenAvatar } from "./avatars";
import { TINTS, TINT_COLORS } from "./themeConfig";
import { themeColors } from "./badges/tokens";
import { rgbHex } from "./themePalette";
import { bytesToHex } from "./encoding";
import vectorsText from "../../tests/fixtures/avatar_vectors.json?raw";

const sha = (s: string) => bytesToHex(sha256(new TextEncoder().encode(s)));
// The same file the Python generators are checked against (tests/test_avatars.py): the two languages draw the same bytes.
const { vectors } = JSON.parse(vectorsText) as { vectors: { id: string; token: string }[] };

describe("the picture palette is the dashboard's", () => {
  it("every tint row is the tint, the dark theme's page background, the light theme's page background and accent, the dark theme's accent", () => {
    TINTS.forEach((t, i) => {
      const light = themeColors("animatic", "light", t), dark = themeColors("animatic", "dark", t);
      expect(TINT[i].slice(0, 5), t).toEqual([TINT_COLORS[t], rgbHex(dark.surfaces[0].rgb), rgbHex(light.surfaces[0].rgb), rgbHex(light.accent), rgbHex(dark.accent)]);
    });
  });
});

describe("the agent pictures", () => {
  it("has the 64 shared vectors", () => expect(vectors).toHaveLength(64));
  it("draws the token picture the shared vectors name, for every id", () => {
    for (const v of vectors) expect(sha(tokenAvatar(v.id))).toBe(v.token);
  });
  it("is plain SVG: no script, text, outside reference or size", () => {
    for (const v of vectors) for (const svg of [tokenAvatar(v.id)]) {
      expect(svg).toMatch(/^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg" viewBox="0 0 32 32">/);
      for (const bad of ["<script", "<text", "<image", "href", "<filter", " width=\"32", "onload"]) expect(svg).not.toContain(bad);
    }
  });
});
