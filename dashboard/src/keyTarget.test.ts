// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { focusedWithin, isTyping, isTypingTarget } from "./keyTarget";

// Section hotkeys must not fire while someone types. Inside the Hermes tab the
// app is in a closed shadow root, where a window listener sees the host as the
// target and cannot look inside — so the app asks its own root what has focus.
describe("section hotkeys and typing", () => {
  it("treats inputs, text areas, selects and editable text as typing", () => {
    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "true");
    for (const el of [document.createElement("input"), document.createElement("textarea"),
      document.createElement("select"), editable]) {
      expect(isTypingTarget(el)).toBe(true);
    }
    expect(isTypingTarget(document.createElement("button"))).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
  });

  it("sees typing in an input inside a closed shadow root", () => {
    const host = document.createElement("div");
    document.body.append(host);
    const shadow = host.attachShadow({ mode: "closed" });
    const app = document.createElement("div");
    const input = document.createElement("input");
    app.append(input);
    shadow.append(app);
    input.focus();

    let typing: boolean | null = null;
    let target: EventTarget | null = null;
    const onKey = (e: KeyboardEvent) => { target = e.target; typing = isTyping(e, app); };
    window.addEventListener("keydown", onKey);
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "2", bubbles: true, composed: true }));
    window.removeEventListener("keydown", onKey);

    expect(target).toBe(host); // what the window listener is given
    expect(focusedWithin(app)).toBe(input); // what the app can still find
    expect(typing).toBe(true);
  });

  it("is not typing when nothing editable has focus", () => {
    const app = document.createElement("div");
    document.body.append(app);
    (document.activeElement as HTMLElement | null)?.blur?.();
    expect(isTyping({ target: document.body }, app)).toBe(false);
  });
});
