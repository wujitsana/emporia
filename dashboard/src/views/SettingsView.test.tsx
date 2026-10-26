// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { groupItems, type SettingItem } from "../settings";

const item = (group: string, label: string): SettingItem =>
  ({ group, label, value: "", about: "", command: "", level: "ok", person_only: false });

describe("P178 settings", () => {
  it("keeps groups in the order the console sent them", () => {
    const groups = groupItems([item("Relay", "a"), item("Role", "b"), item("Relay", "c")]);
    expect(groups.map(([g, l]) => [g, l.map((i) => i.label)])).toEqual([["Relay", ["a", "c"]], ["Role", ["b"]]]);
  });
});
