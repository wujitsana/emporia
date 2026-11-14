import { describe, expect, it } from "vitest";
import { friendlyError } from "./friendlyError";

describe("friendlyError", () => {
  it("turns a network failure into a sentence", () => {
    expect(friendlyError("TypeError: Failed to fetch")).toBe("Can't reach the relay. Try again in a moment.");
  });
  it("says a refused request by its status, never its path", () => {
    expect(friendlyError("Error: 500 /payments/records?mine=1")).toBe("The relay answered with an error (500). Try again in a moment.");
  });
  it("says a relay that did not answer as can't reach, not as an error of the relay", () => {
    expect(friendlyError("Error: 502 /agents/scout/inbox")).toBe("Can't reach the relay. Try again in a moment.");
  });
  it("never shows a transport class name", () => {
    expect(friendlyError("GatewayReauthRequiredError: Run hermes webapp on this computer to sign this browser in.")).toBe("Something went wrong loading this. Try reopening the page.");
  });
  it("gives a script error a plain sentence, not its message", () => {
    expect(friendlyError("TypeError: Cannot read properties of undefined (reading 'x')")).toBe("Something went wrong; try again.");
    expect(friendlyError("Error: bad thing")).toBe("Something went wrong; try again.");
  });
  it("leaves any other message as written", () => {
    expect(friendlyError("not found")).toBe("not found");
  });
});
