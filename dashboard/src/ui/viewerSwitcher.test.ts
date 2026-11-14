import { describe, expect, it } from "vitest";
import { selectWidth } from "./ViewerSwitcher";

describe("the header's agent select", () => {
  it("is as wide as the name it shows: 20 characters whole, then it stops growing (an ellipsis)", () => {
    expect(selectWidth("wujitsana")).toBe("12ch");
    expect(selectWidth("agent-name-twenty-ch")).toBe("23ch");
    expect(selectWidth("a-very-long-agent-name-that-goes-on")).toBe("23ch");
    expect(selectWidth("a")).toBe("9ch");
  });
});
