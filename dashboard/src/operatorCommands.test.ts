import { describe, expect, it } from "vitest";
import {
  bytes, expeditionCommand, houseCommands, labCommands, snapshotCommand, uptime,
  type OperatorCommand,
} from "./operatorCommands";
// The table emporia/operator_desk.py reproduces too (tests/test_operator.py): one set of strings.
import vectors from "./operatorCommands.vectors.json";

interface Case {
  kind: string;
  name: string;
  item?: { lab?: string };
  running?: boolean;
  expect: OperatorCommand[];
}

function built(c: Case): OperatorCommand[] {
  if (c.kind === "expedition") return expeditionCommand(c.name);
  if (c.kind === "house") return houseCommands(c.name, c.running);
  if (c.kind === "lab") return labCommands(c.name, c.item?.lab);
  return snapshotCommand(c.name);
}

describe("the operator's commands, as the Python side builds them", () => {
  it("reproduces every case in the shared table", () => {
    const cases = vectors.cases as Case[];
    expect(cases.length).toBeGreaterThanOrEqual(8);
    for (const c of cases) expect([c.kind, c.name, built(c)]).toEqual([c.kind, c.name, c.expect]);
  });

  it("puts nothing in a command but a checked id, the profile and placeholders", () => {
    expect(labCommands("op", "`id`")).toEqual([]);
    expect(labCommands("Op; rm", "emporia")).toEqual([]);
  });

  it("reads a lab with a plain command and refuses an unsafe name", () => {
    expect(labCommands("op", "emporia")[0].command).toBe("hermes emporia lab op show emporia");
    expect(labCommands("op", "emporia; rm -rf ~")).toEqual([]);
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
