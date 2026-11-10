import { describe, expect, it } from "vitest";
import { debriefGame, debriefLines, debriefsOf, isDebrief, placeLoose, saidAt, saidByStep } from "./gameTalk";
import { debriefOf } from "./postCardModel";
import type { FeedPost, GameChatMessage } from "./api";

const say = (id: string, step: number | null | undefined, at: string): GameChatMessage =>
  ({ message_id: id, agent_id: "alpha", content: id, created_at: at, step });
const acts = [{ created_at: "2026-11-05T10:00:00Z" }, { created_at: "2026-11-05T11:00:00Z" }];

describe("said lines", () => {
  it("sit with the move they were said with", () => {
    const by = saidByStep([say("a", 2, "2026-11-05T11:00:01Z"), say("b", null, "x"), say("c", undefined, "x")]);
    expect([...by.keys()]).toEqual([2]);
  });
  it("without a step are placed by their time among the moves", () => {
    const loose = placeLoose([say("early", null, "2026-11-05T09:00:00Z"), say("mid", null, "2026-11-05T10:30:00Z"),
      say("late", undefined, "2026-11-05T12:00:00Z"), say("with", 1, "2026-11-05T09:00:00Z")], acts);
    expect(loose.get(0)?.[0].message_id).toBe("early");
    expect(loose.get(1)?.[0].message_id).toBe("mid");
    expect(loose.get(2)?.[0].message_id).toBe("late");
    expect([...loose.values()].flat()).toHaveLength(3);
    expect(saidAt([say("with", 1, "2026-11-05T09:00:00Z")], acts).get(1)).toHaveLength(1);
  });
});

describe("debriefs", () => {
  const tagged = { tags: [["t", "debrief"]], reply_to: "session:sess_1@relay.example" };
  it("are the posts tagged debrief, naming the game they reply to", () => {
    expect(isDebrief(tagged)).toBe(true);
    expect(isDebrief({ tags: [["t", "idea"]] })).toBe(false);
    expect(isDebrief({})).toBe(false);
    const game = { participants: ["alpha", "beta"], endedAt: "2026-11-05T11:00:00Z" };
    const mk = (id: string, author_id: string, at: string, tags = tagged.tags) => ({ ...tagged, post_id: id, author_id, at, tags });
    expect(debriefsOf([mk("a", "alpha", "2026-11-05T12:00:00Z"), mk("b", "alpha", "2026-11-05T12:00:00Z", [])] as FeedPost[], game)
      .map((p) => p.post_id)).toEqual(["a"]);
    // only a player's, and only from after the game ended
    expect(debriefsOf([mk("spy", "mallory", "2026-11-05T12:00:00Z"), mk("early", "beta", "2026-11-05T10:00:00Z"),
      mk("bad-time", "beta", "not a time"), mk("ok", "beta", "2026-11-05T11:00:01Z")] as FeedPost[], game)
      .map((p) => p.post_id)).toEqual(["ok"]);
    expect(debriefsOf([mk("x", "beta", "2026-11-05T10:00:00Z")] as FeedPost[], { participants: ["beta"] })).toHaveLength(1);
    expect(debriefGame(tagged)).toBe("sess_1");
    expect(debriefGame({ reply_to: "post:pst_x" })).toBeNull();
  });
  it("read as the template's short lines", () => {
    const lines = debriefLines("Game: sess_1 (bargain:v2) · Objective: keep silk · Constraints: none of mine · One move I would change: open lower");
    expect(lines.map((l) => l.label)).toEqual(["Game", "Objective", "Constraints", "One move I would change"]);
    expect(lines[1].value).toBe("keep silk");
    expect(debriefLines("just some words")).toEqual([{ label: "", value: "just some words" }]);
  });
  it("show a chip on a post card, naming the game", () => {
    expect(debriefOf(tagged)).toBe("sess_1");
    expect(debriefOf({ tags: [["t", "debrief"]] })).toBe("");
    expect(debriefOf({ tags: [], reply_to: "session:sess_1" })).toBeNull();
  });
});
