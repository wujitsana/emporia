// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Tint } from "./themeConfig";
import { ThemeCompact, ThemePanelBody } from "./ThemeControls";

afterEach(cleanup);

function setup(theme: "theme-light" | "theme-vivid" | "theme-dark", base?: "theme-light" | "theme-dark", tint: Tint = "tint-green") {
  const onTheme = vi.fn();
  const onTint = vi.fn();
  render(<ThemePanelBody theme={theme} base={base} tint={tint} onTheme={onTheme} onTint={onTint} />);
  return { onTheme, onTint };
}

describe("the theme control", () => {
  it("has three circles in order: mode, vivid, tint", () => {
    setup("theme-light");
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(3);
    expect(buttons[0].getAttribute("aria-label")).toBe("Theme: light, switch to dark");
    expect(buttons[1].getAttribute("aria-label")).toBe("Vivid: off, switch on");
    expect(buttons[2].getAttribute("aria-label")).toBe("Accent color: Green, click to cycle");
    expect(buttons[0].className).toContain("e-theme-circle--mode");
    expect(buttons[1].className).toContain("e-theme-circle--vivid");
    expect(buttons[2].className).toContain("e-theme-circle--accent");
  });

  it("the mode circle swaps light and dark", () => {
    const { onTheme } = setup("theme-dark");
    const mode = screen.getByLabelText("Theme: dark, switch to light");
    expect(mode.getAttribute("title")).toBe("dark (click for light)");
    fireEvent.click(mode);
    expect(onTheme).toHaveBeenCalledWith("theme-light");
  });

  it("the vivid circle turns vivid on, and off back to the last of light / dark", () => {
    const a = setup("theme-dark", "theme-dark");
    fireEvent.click(screen.getByLabelText("Vivid: off, switch on"));
    expect(a.onTheme).toHaveBeenCalledWith("theme-vivid");
    cleanup();
    const b = setup("theme-vivid", "theme-dark");
    const on = screen.getByLabelText("Vivid: on, switch to dark");
    expect(on.getAttribute("aria-pressed")).toBe("true");
    expect(on.getAttribute("title")).toBe("vivid on (click for dark)");
    fireEvent.click(on);
    expect(b.onTheme).toHaveBeenCalledWith("theme-dark");
  });

  it("in vivid the mode circle shows the last mode and a click leaves vivid for the other", () => {
    const { onTheme } = setup("theme-vivid", "theme-light");
    fireEvent.click(screen.getByLabelText("Theme: light, switch to dark"));
    expect(onTheme).toHaveBeenCalledWith("theme-dark");
  });

  it("the tint circle goes to the next tint, wrapping to green after charcoal", () => {
    const a = setup("theme-light", "theme-light", "tint-green");
    fireEvent.click(screen.getByLabelText("Accent color: Green, click to cycle"));
    expect(a.onTint).toHaveBeenCalledWith("tint-blue");
    cleanup();
    const b = setup("theme-light", "theme-light", "tint-charcoal");
    fireEvent.click(screen.getByLabelText("Accent color: Charcoal, click to cycle"));
    expect(b.onTint).toHaveBeenCalledWith("tint-green");
  });

  it("the compact (header / phone) control has the same three circles", () => {
    render(<ThemeCompact theme="theme-vivid" tint="tint-blue" onTheme={() => {}} onTint={() => {}} />);
    expect(screen.getAllByRole("button")).toHaveLength(3);
  });
});
