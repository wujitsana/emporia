import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("./dashboard.css", import.meta.url), "utf8");
const kit = readFileSync(new URL("./ui/kit/global.css", import.meta.url), "utf8");

describe("a post card's reply rows", () => {
  it("do not inherit the kit's hanging indent, which pulled the author name under its avatar", () => {
    expect(kit).toMatch(/ul\s*\{[^}]*text-indent:\s*-1ch/);
    expect(css).toMatch(/\.e-post-card__replies[^{]*\{\s*text-indent:\s*0;?\s*\}/);
  });

  it("keep the tag and time from sliding over the name: only the name shrinks", () => {
    expect(css).toMatch(/\.e-post-card__who > \* \{ flex: none; \}/);
    expect(css).toMatch(/\.e-post-card__name \{[^}]*text-overflow: ellipsis/);
  });
});
