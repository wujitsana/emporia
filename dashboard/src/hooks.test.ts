// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useInterval, useRoomWs, useSenderPubkeys } from "./hooks";

// P24-5 (W5, F2): useRoomWs (hooks.ts) only cleared `messages` when `roomId`
// went falsy, not on every roomId change -- so switching room A -> room B
// rendered room A's messages, still tagged with room A's ids, against room
// B's verification context until room B's room_init frame arrived. This is
// this codebase's first React hook test; jsdom's real WebSocket would
// attempt actual network connections, so a hand-written test double stands
// in for it (stubbed via vi.stubGlobal), and the environment is scoped to
// just this file via the docblock above rather than changed globally --
// api.test.ts and integrityCheck.test.ts need no DOM.

class FakeWebSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 3;

  static instances: FakeWebSocket[] = [];

  url: string;
  readyState = FakeWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(_data: string) {}

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  // test helpers, not part of the real WebSocket API
  fireOpen() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  fireMessage(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useRoomWs", () => {
  it("clears stale messages immediately when roomId changes, before the new room's room_init arrives", () => {
    const { result, rerender } = renderHook(
      ({ roomId }: { roomId: string | null }) => useRoomWs("http://relay.example", roomId, "agent-1"),
      { initialProps: { roomId: "room-a" } },
    );

    const wsA = FakeWebSocket.instances[0];
    act(() => wsA.fireOpen());
    act(() =>
      wsA.fireMessage({
        type: "room_init",
        messages: [{ message_id: "m1", sender_id: "s1", msg_type: "chat", content: "hi", created_at: "t" }],
      }),
    );

    expect(result.current.messages).toHaveLength(1);

    // switch rooms -- room B's room_init hasn't arrived yet
    rerender({ roomId: "room-b" });

    // the P23 regression: without the fix, room A's message is still here,
    // now rendered against room B's context, causing a false unverified chip
    expect(result.current.messages).toHaveLength(0);
    expect(result.current.connected).toBe(false);
  });

  it("still clears on the falsy-roomId transition (the case that already worked)", () => {
    const { result, rerender } = renderHook(
      ({ roomId }: { roomId: string | null }) => useRoomWs("http://relay.example", roomId, "agent-1"),
      { initialProps: { roomId: "room-a" as string | null } },
    );

    const wsA = FakeWebSocket.instances[0];
    act(() => wsA.fireOpen());
    act(() =>
      wsA.fireMessage({
        type: "room_init",
        messages: [{ message_id: "m1", sender_id: "s1", msg_type: "chat", content: "hi", created_at: "t" }],
      }),
    );
    expect(result.current.messages).toHaveLength(1);

    rerender({ roomId: null });

    expect(result.current.messages).toHaveLength(0);
    expect(result.current.connected).toBe(false);
  });
});

describe("useSenderPubkeys", () => {
  it("requests each distinct sender's pubkey at most once, even across many re-renders with a new array identity each time", async () => {
    const fetchPubkey = vi.fn((id: string) => Promise.resolve(`key-${id}`));
    const { result, rerender } = renderHook(
      ({ senders }: { senders: string[] }) => useSenderPubkeys(senders, fetchPubkey),
      { initialProps: { senders: ["a"] } },
    );

    // simulate a burst of WS frames -- each rerender passes a brand-new array
    // (as `messages.map(...)` would), with the sender set only growing by one.
    // The P23 bug re-ran the fetch effect (and could re-request in-flight ids)
    // on every one of these, not just when the sender *set* actually changed.
    for (let i = 0; i < 20; i++) {
      rerender({ senders: ["a", "b"] });
    }

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchPubkey).toHaveBeenCalledTimes(2);
    expect(fetchPubkey).toHaveBeenCalledWith("a");
    expect(fetchPubkey).toHaveBeenCalledWith("b");
    expect(result.current).toEqual({ a: "key-a", b: "key-b" });
  });

  it("does not re-request an id whose fetch already failed and cached empty", async () => {
    const fetchPubkey = vi.fn(() => Promise.resolve(""));
    const { rerender } = renderHook(
      ({ senders }: { senders: string[] }) => useSenderPubkeys(senders, fetchPubkey),
      { initialProps: { senders: ["a"] } },
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    rerender({ senders: ["a"] });
    rerender({ senders: [...["a"]] }); // fresh array identity, same set

    expect(fetchPubkey).toHaveBeenCalledTimes(1);
  });
});


describe("useInterval stale-data semantics (P33-2)", () => {
  it("keeps the previous subject's data across a deps change, which is why the chain badges need a key", async () => {
    // This is a *documented* property, not a bug: EventsView/ListingsView rely
    // on it so a manual refresh doesn't blank the list mid-request. But it is
    // exactly why RoomChainBadge carries `key={selected.room_id}` (P24-5, W5,
    // F2) and DmChainBadge `key={active.thread_id}` (P33-2) at their call
    // sites: without a remount, switching subject renders the OLD subject's
    // verdict against the NEW subject's id -- a false "✗ unverified" on an
    // innocent room or thread, the same user-visible false alarm P24 fixed.
    //
    // Pinned here so the coupling is explicit in both directions: if
    // useInterval ever starts clearing on a dep change, this test fails and
    // whoever changes it is pointed straight at the two `key` props that exist
    // only because of this behaviour.
    let resolveSecond: ((v: string) => void) | undefined;
    const fn = vi.fn((subject: string) =>
      subject === "first"
        ? Promise.resolve("first-result")
        : new Promise<string>((res) => { resolveSecond = res; }),
    );

    const { result, rerender } = renderHook(
      ({ subject }: { subject: string }) => useInterval(() => fn(subject), 60_000, [subject]),
      { initialProps: { subject: "first" } },
    );

    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current[0]).toBe("first-result");

    // Switch subject; the new fetch is still in flight.
    rerender({ subject: "second" });
    await act(async () => { await Promise.resolve(); });
    expect(result.current[0]).toBe("first-result");

    // Only once the new fetch resolves does the value catch up.
    await act(async () => {
      resolveSecond?.("second-result");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current[0]).toBe("second-result");
  });
});
