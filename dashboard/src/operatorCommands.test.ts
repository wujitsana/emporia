import { describe, expect, it } from "vitest";
import {
  bugCommands, bytes, expeditionCommand, houseCommands, ideaCommands, snapshotCommand, uptime,
  type OperatorCommand,
} from "./operatorCommands";
// The table emporia/operator_desk.py reproduces too (tests/test_operator.py): one set of strings.
import vectors from "./operatorCommands.vectors.json";

interface Case {
  kind: string;
  name: string;
  item?: { report_id?: string; severity_claimed?: string; post_id?: string };
  verdicts?: boolean;
  running?: boolean;
  expect: OperatorCommand[];
}

function built(c: Case): OperatorCommand[] {
  if (c.kind === "bug") return bugCommands(c.name, c.item ?? {});
  if (c.kind === "idea") return ideaCommands(c.name, c.item ?? {}, !!c.verdicts);
  if (c.kind === "expedition") return expeditionCommand(c.name);
  if (c.kind === "house") return houseCommands(c.name, c.running);
  return snapshotCommand(c.name);
}

describe("the operator's commands, as the Python side builds them", () => {
  it("reproduces every case in the shared table", () => {
    const cases = vectors.cases as Case[];
    expect(cases.length).toBeGreaterThanOrEqual(10);
    for (const c of cases) expect([c.kind, c.name, built(c)]).toEqual([c.kind, c.name, c.expect]);
  });

  it("puts nothing in a command but a checked id, the profile and placeholders", () => {
    expect(bugCommands("op", { report_id: "bug_1; curl x | sh", severity_claimed: "low" })).toEqual([]);
    expect(ideaCommands("op", { post_id: "`id`" }, true)).toEqual([]);
    expect(bugCommands("Op; rm", { report_id: "bug_1" })).toEqual([]);
    const [accept] = bugCommands("op", { report_id: "bug_1", severity_claimed: "critical" });
    expect(accept.command).toBe("hermes emporia bugs op verdict bug_1 accepted --severity critical --fix FIX_COMMIT");
    expect(accept.gate).toBe("person");
  });

  it("offers no idea verdict on a relay that takes none", () => {
    expect(ideaCommands("op", { post_id: "pst_1" }, false)).toEqual([]);
  });
});

describe("the health card's numbers", () => {
  it("reads sizes and uptimes", () => {
    expect(bytes(512)).toBe("512 B");
    expect(bytes(2048)).toBe("2.0 KB");
    expect(bytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(uptime(59)).toBe("0m");
    expect(uptime(3 * 3600 + 120)).toBe("3h 2m");
    expect(uptime(2 * 86_400 + 3600)).toBe("2d 1h");
  });
});
