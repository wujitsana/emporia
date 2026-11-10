import { describe, expect, it } from "vitest";
import { bareAgentName, isVisitorAddress, setOwnHost } from "./agentName";
import { toolLabel } from "./chat/chatModel";
import { kindChip, relativeTime } from "./postCardModel";

describe("agent names", () => {
  it("are bare on this relay, name@host for a visitor, and never carry a port", () => {
    setOwnHost("127.0.0.1:18741");
    expect(bareAgentName("ali@127.0.0.1:18741")).toBe("ali");
    expect(bareAgentName("ali")).toBe("ali");
    expect(bareAgentName("zed@other.example:8088")).toBe("zed@other.example");
    expect(bareAgentName("zed@other.example")).toBe("zed@other.example");
    expect(isVisitorAddress("zed@other.example:8088")).toBe(true);
    expect(isVisitorAddress("ali@127.0.0.1:18741")).toBe(false);
  });
});

describe("tool names in the chat", () => {
  it("read as 'emporia · get session' in both Hermes spellings", () => {
    expect(toolLabel("mcp__emporia__get_session")).toBe("emporia · get session");
    expect(toolLabel("mcp_emporia_get_session")).toBe("emporia · get session");
    expect(toolLabel("terminal")).toBe("terminal");
  });
});

describe("post card text", () => {
  it("says how long ago, and the kind chip names an 'other' kind by its label", () => {
    const now = Date.parse("2026-11-10T12:00:00Z");
    expect(relativeTime("2026-11-10T11:58:00Z", now)).toBe("2m ago");
    expect(relativeTime("2026-11-10T09:00:00Z", now)).toBe("3h ago");
    expect(relativeTime("2026-11-09T09:00:00Z", now)).toBe("yesterday");
    expect(relativeTime("2026-11-07T12:00:00Z", now)).toBe("3d ago");
    expect(kindChip("idea")).toBe("idea");
    expect(kindChip("other", "recipe")).toBe("other: recipe");
  });
});
