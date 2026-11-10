// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../transport", async (orig) => ({ ...(await orig<object>()), MODE: "console" }));

import { AskAgent } from "./AskAgent";
import { standaloneQuestion } from "./standaloneQuestion";

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__EMPORIA_HERMES_SDK__;
});

describe("ask your agent in the standalone console", () => {
  it("copies a complete question and says where to paste it", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<AskAgent prompt="About blotto session sess_1 (finished): " />);
    expect(screen.getByText(/Paste it in your agent's own chat \(Claude Desktop, Codex/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /ask your agent/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const text = writeText.mock.calls[0][0] as string;
    expect(text).toContain("About blotto session sess_1 (finished): ");
    expect(text.trimEnd().endsWith("?")).toBe(true);
    await waitFor(() => expect(screen.getByRole("button", { name: /copied/ })).toBeTruthy());
  });

  it("leaves a finished question as it is", () => {
    expect(standaloneQuestion("Why did you decline?")).toBe("Why did you decline?");
  });
});
