// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { McpCommandHint } from "./McpCommandHint";

afterEach(cleanup);

// Review pass 2: the command an agent would run copied on a click of the <code> only, which a
// keyboard cannot reach; and "copied" was not announced.
describe("the MCP command hint", () => {
  it("copies from a button a keyboard reaches, and says it copied", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<McpCommandHint label="join" tool="join_session" args={{ session_id: "s-1" }} />);
    const button = screen.getByRole("button", { name: 'Copy: join_session(session_id="s-1")' });
    await act(async () => { fireEvent.click(button); });
    expect(writeText).toHaveBeenCalledWith('join_session(session_id="s-1")');
    expect(screen.getByRole("status").textContent).toBe("copied");
  });
});
