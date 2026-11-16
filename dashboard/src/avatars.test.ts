import { sha256 } from "@noble/hashes/sha2.js";
import { describe, expect, it } from "vitest";
import { everydayAvatar, tokenAvatar } from "./avatars";
import { bytesToHex } from "./encoding";
import vectorsText from "../../tests/fixtures/avatar_vectors.json?raw";

const sha = (s: string) => bytesToHex(sha256(new TextEncoder().encode(s)));
// The same file the Python generators are checked against (tests/test_avatars.py): the two languages draw the same bytes.
const { vectors } = JSON.parse(vectorsText) as { vectors: { id: string; token: string; everyday: string }[] };

describe("the agent pictures", () => {
  it("has the 64 shared vectors", () => expect(vectors).toHaveLength(64));
  it("draws the token picture the shared vectors name, for every id", () => {
    for (const v of vectors) expect(sha(tokenAvatar(v.id))).toBe(v.token);
  });
  it("draws the everyday picture the shared vectors name, for every id", () => {
    for (const v of vectors) expect(sha(everydayAvatar(v.id))).toBe(v.everyday);
  });
  it("takes a suffix for the everyday picture's two pattern ids and changes nothing else", () => {
    const id = vectors[0].id;
    const a = everydayAvatar(id, "one"), b = everydayAvatar(id, "two");
    expect(a).toContain('id="aone"');
    expect(b).toContain('id="btwo"');
    expect(a.replaceAll("one", "two")).toBe(b);
  });
  it("is plain SVG: no script, text, outside reference or size", () => {
    for (const v of vectors) for (const svg of [tokenAvatar(v.id), everydayAvatar(v.id)]) {
      expect(svg).toMatch(/^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg" viewBox="0 0 32 32">/);
      for (const bad of ["<script", "<text", "<image", "href", "<filter", " width=\"32", "onload"]) expect(svg).not.toContain(bad);
    }
  });
});
