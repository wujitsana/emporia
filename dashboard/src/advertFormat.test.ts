import { describe, expect, it } from "vitest";

import { feeLabel } from "./advertFormat";

describe("fee labels", () => {
  it("writes a fee in words", () => {
    expect(feeLabel(0)).toBe("free");
    expect(feeLabel(undefined)).toBe("free");
    expect(feeLabel(500, "USD")).toBe("$5.00");
    expect(feeLabel(500, "eur")).toBe("$5.00 EUR");
  });
});
