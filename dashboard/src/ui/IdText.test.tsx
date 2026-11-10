// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { IdText } from "./ArenaMatch";

describe("IdText", () => {
  it("shows the short text and keeps the full value in the title", () => {
    render(<IdText text="…abcdef12" full="session-0123456789abcdef12" />);
    const el = screen.getByText("…abcdef12");
    expect(el.getAttribute("title")).toBe("session-0123456789abcdef12");
    expect(el.className).toBe("e-id");
  });
});
