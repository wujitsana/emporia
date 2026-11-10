// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ReadOnlyChat } from "./ReadOnlyChat";

afterEach(cleanup);

describe("a chat line's facts", () => {
  it("shows the declared model and 'opened here' as small chips beside the time, not in the words", () => {
    render(
      <ReadOnlyChat viewerId="alpha" messages={[
        { id: "1", sender_id: "beta", content: "What do you trade?", created_at: "2026-11-10T12:04:00Z", model: "gpt-5.6-terra", note: "opened here" },
        { id: "2", sender_id: "alpha", content: "encrypted — not readable here", created_at: "2026-11-10T12:05:00Z", note: "sealed" },
      ]} />,
    );
    expect(screen.getByText("opened here")).toBeTruthy();
    expect(screen.getByText("model gpt-5.6-terra")).toBeTruthy();
    expect(screen.getByText("sealed")).toBeTruthy();
    expect(screen.getByText("What do you trade?").textContent).not.toMatch(/opened|\[|gpt/);
  });
});
