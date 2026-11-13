import { describe, expect, it } from "vitest";
import { postCardShape } from "./homeCardsModel";

// One shape for every Home post card: author = head, kind = chip (none for a plain note), topic = caption, likes = foot.
describe("postCardShape", () => {
  const table: [string, { kind: string; label?: string; likes?: number }, string | undefined, string | undefined][] = [
    ["note", { kind: "note" }, undefined, undefined],
    ["post", { kind: "post" }, undefined, undefined],
    ["text", { kind: "text", likes: 1 }, undefined, "1 like"],
    ["other, unlabelled", { kind: "other" }, undefined, undefined],
    ["other, labelled", { kind: "other", label: "riddle", likes: 3 }, "RIDDLE", "3 likes"],
    ["idea", { kind: "idea" }, "IDEA", undefined],
    ["bug", { kind: "bug", likes: 2 }, "BUG", "2 likes"],
  ];
  it.each(table)("%s", (_n, a, chip, foot) => {
    const s = postCardShape({ ...a, who: "bob", topicName: "Ideas" });
    expect(s).toEqual({ head: "bob", chip, caption: "Ideas", foot });
  });
  it("an absent topic is an empty caption, never the author", () => {
    expect(postCardShape({ kind: "idea", who: "bob" }).caption).toBe("");
  });
});
