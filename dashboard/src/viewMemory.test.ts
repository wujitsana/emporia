// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { recall, recallOf, remember } from "./viewMemory";

afterEach(() => { vi.restoreAllMocks(); window.sessionStorage.clear(); });

describe("viewMemory", () => {
  it("keeps a choice and only gives back an allowed one", () => {
    remember("feed.tab", "post");
    expect(recall("feed.tab")).toBe("post");
    expect(recallOf("feed.tab", ["", "post"] as const, "")).toBe("post");
    remember("feed.tab", "gone");
    expect(recallOf("feed.tab", ["", "post"] as const, "")).toBe("");
  });

  it("works without storage", () => {
    const real = Object.getOwnPropertyDescriptor(window, "sessionStorage");
    Object.defineProperty(window, "sessionStorage", { configurable: true, get() { throw new Error("blocked"); } });
    try {
      expect(() => remember("a", "b")).not.toThrow();
      expect(recall("a")).toBeNull();
      expect(recallOf("a", ["x"] as const, "x")).toBe("x");
    } finally {
      if (real) Object.defineProperty(window, "sessionStorage", real);
    }
  });
});
