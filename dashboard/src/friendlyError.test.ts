import { describe, expect, it } from "vitest";
import { friendlyError } from "./friendlyError";

describe("friendlyError", () => {
  it("turns a network failure into a sentence", () => {
    expect(friendlyError("TypeError: Failed to fetch")).toBe("Can't reach the relay. Try again in a moment.");
  });
  it("says a refused request by its status, never its path", () => {
    expect(friendlyError("Error: 500 /payments/records?mine=1")).toBe("The relay answered with an error (500). Try again in a moment.");
  });
  it("leaves any other message as written", () => {
    expect(friendlyError("not found")).toBe("not found");
  });
});
