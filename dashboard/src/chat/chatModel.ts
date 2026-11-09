/**
 * The chat transcript as a list of items, built from Hermes gateway events.
 * Pure functions only, so the whole flow is testable without a socket.
 */
import { redactSecrets } from "./redact";

export type ToolStatus = "running" | "done" | "failed" | "blocked";

export type ChatItem =
  | { kind: "user"; id: string; text: string }
  | { kind: "agent"; id: string; text: string; streaming: boolean }
  /** A relay event as the agent received it (a relay-session turn): other agents' words, untrusted. */
  | { kind: "relay"; id: string; text: string }
  | {
      kind: "tool";
      id: string;
      name: string;
      context: string;
      status: ToolStatus;
      output: string;
      duration?: number;
      /** A tool that brings relay content in (Emporia's own tools): what it returns came from other agents. */
      relay: boolean;
    }
  | {
      kind: "approval";
      id: string;
      requestId: string;
      command: string;
      description: string;
      choices: string[];
      /** An Emporia command that changes keys, wallets, limits or a role (key_guard). */
      emporia: boolean;
      answered?: string;
    }
  | {
      kind: "clarify";
      id: string;
      requestId: string;
      questions: { qid: string; question: string; choices: string[] }[];
      answered?: boolean;
    }
  | { kind: "notice"; id: string; text: string; tone: "info" | "error" | "command" }
  | { kind: "declined"; id: string; what: string };

export interface ChatState {
  items: ChatItem[];
  busy: boolean;
  title: string;
}

export const EMPTY: ChatState = { items: [], busy: false, title: "" };

let n = 0;
export const nextId = (p: string) => `${p}-${++n}`;

/** Tools whose results carry other agents' words: Emporia's MCP tools (`mcp__emporia__*` since Hermes 0.23;
 * `mcp_emporia_*` in older transcripts). */
export function isRelayTool(name: string): boolean {
  return /^mcp__emporia__/i.test(name) || /^mcp_emporia(_|$)/i.test(name) || /^emporia[_.]/i.test(name);
}

/** key_guard's approval texts (hermes_plugin/emporia/key_guard.py: APPROVE, RELAY_CONTROL). */
export function isEmporiaApproval(description: string): boolean {
  return /^Emporia: the agent wants to run (?:a command|one of the relay operator)/i.test(description);
}

function str(v: unknown): string {
  if (typeof v === "string") return v;
  if (v == null) return "";
  try {
    return JSON.stringify(v, null, 1);
  } catch {
    return String(v);
  }
}

function toolOutput(result: unknown): { text: string; failed: boolean; blocked: boolean } {
  if (result && typeof result === "object") {
    const r = result as Record<string, unknown>;
    const err = typeof r.error === "string" ? r.error : "";
    const out = typeof r.output === "string" ? r.output : err ? "" : str(result);
    const blocked = /^BLOCKED:|Blocked by Emporia/.test(err) || /Blocked by Emporia/.test(out);
    const failed = !!err || (typeof r.exit_code === "number" && r.exit_code !== 0);
    return { text: redactSecrets([out, err].filter(Boolean).join("\n")), failed, blocked };
  }
  const text = redactSecrets(str(result));
  return { text, failed: false, blocked: /Blocked by Emporia/.test(text) };
}

function lastAgent(items: ChatItem[]): number {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "agent" && it.streaming) return i;
    if (it.kind === "user") return -1;
  }
  return -1;
}

/** Apply one gateway event to the transcript. */
export function applyEvent(state: ChatState, type: string, payload: Record<string, unknown> = {}): ChatState {
  const items = state.items;
  switch (type) {
    case "message.start":
      return { ...state, busy: true };
    case "message.delta": {
      const text = typeof payload.text === "string" ? payload.text : "";
      if (!text) return state;
      const i = lastAgent(items);
      if (i >= 0) {
        const it = items[i] as Extract<ChatItem, { kind: "agent" }>;
        const copy = items.slice();
        copy[i] = { ...it, text: it.text + text };
        return { ...state, items: copy, busy: true };
      }
      return { ...state, busy: true, items: [...items, { kind: "agent", id: nextId("a"), text, streaming: true }] };
    }
    case "message.complete": {
      const finalText = typeof payload.text === "string" ? payload.text : "";
      const i = lastAgent(items);
      const copy = items.slice();
      if (i >= 0) {
        const it = copy[i] as Extract<ChatItem, { kind: "agent" }>;
        copy[i] = { ...it, text: finalText || it.text, streaming: false };
      } else if (finalText) {
        copy.push({ kind: "agent", id: nextId("a"), text: finalText, streaming: false });
      }
      const err = typeof payload.error === "string" ? payload.error : "";
      if (err) copy.push({ kind: "notice", id: nextId("n"), text: err, tone: "error" });
      if (payload.status === "interrupted") copy.push({ kind: "notice", id: nextId("n"), text: "stopped", tone: "info" });
      return { ...state, busy: false, items: copy };
    }
    case "tool.start": {
      // A tool call closes the agent's text segment before it.
      const copy = items.map((it) => (it.kind === "agent" && it.streaming ? { ...it, streaming: false } : it));
      const name = String(payload.name ?? "tool");
      copy.push({
        kind: "tool",
        id: String(payload.tool_id ?? nextId("t")),
        name,
        context: redactSecrets(String(payload.context ?? payload.preview ?? "")),
        status: "running",
        output: "",
        relay: isRelayTool(name),
      });
      return { ...state, busy: true, items: copy };
    }
    case "tool.complete": {
      const id = String(payload.tool_id ?? "");
      const { text, failed, blocked } = toolOutput(payload.result ?? payload.result_text ?? payload.summary);
      const copy = items.map((it) =>
        it.kind === "tool" && it.id === id
          ? {
              ...it,
              status: (blocked ? "blocked" : failed ? "failed" : "done") as ToolStatus,
              output: text,
              duration: typeof payload.duration_s === "number" ? payload.duration_s : undefined,
            }
          : it,
      );
      return { ...state, items: copy };
    }
    case "session.title": {
      const title = typeof payload.title === "string" ? payload.title : "";
      return title ? { ...state, title } : state;
    }
    case "error": {
      const text = typeof payload.message === "string" ? payload.message : "error";
      return { ...state, busy: false, items: [...items, { kind: "notice", id: nextId("n"), text, tone: "error" }] };
    }
    case "approval.cancelled":
    case "request.cancel": {
      const rid = String(payload.request_id ?? payload.id ?? "");
      return {
        ...state,
        items: items.map((it) =>
          (it.kind === "approval" || it.kind === "clarify") && !it.answered && (!rid || it.requestId === rid)
            ? ({ ...it, answered: it.kind === "approval" ? "withdrawn" : true } as ChatItem)
            : it,
        ),
      };
    }
    default:
      return state;
  }
}

/** A server→client request rendered as a card (approval, clarify) or declined with a note. */
export function applyRequest(state: ChatState, id: string, method: string, params: Record<string, unknown>): ChatState {
  if (method === "approval") {
    const description = redactSecrets(String(params.description ?? ""));
    const emporia = isEmporiaApproval(description);
    const offered = Array.isArray(params.choices) ? params.choices.map(String) : ["once", "deny"];
    // "always" is never offered here: a permanent allow is set in Hermes itself.
    // An Emporia key/wallet/limits/role change gets "once" or "deny" only.
    const choices = offered.filter((c) => c !== "always" && !(emporia && c === "session"));
    return {
      ...state,
      items: [
        ...state.items,
        {
          kind: "approval",
          id: nextId("ap"),
          requestId: id,
          command: redactSecrets(String(params.command ?? "")),
          description,
          choices: choices.includes("deny") ? choices : [...choices, "deny"],
          emporia,
        },
      ],
    };
  }
  if (method === "clarify") {
    const qs = Array.isArray(params.questions) ? (params.questions as Record<string, unknown>[]) : [];
    return {
      ...state,
      items: [
        ...state.items,
        {
          kind: "clarify",
          id: nextId("cl"),
          requestId: id,
          questions: qs.map((q) => ({
            qid: String(q.qid ?? ""),
            question: redactSecrets(String(q.question ?? "")),
            choices: Array.isArray(q.choices) ? q.choices.map(String) : [],
          })),
        },
      ],
    };
  }
  return { ...state, items: [...state.items, { kind: "declined", id: nextId("d"), what: method }] };
}

/** Methods this UI answers. Everything else (sudo, secret, vault, …) is declined: never typed here. */
export const ANSWERED_REQUESTS = new Set(["approval", "clarify"]);

export function markAnswered(state: ChatState, requestId: string, answer: string): ChatState {
  return {
    ...state,
    items: state.items.map((it) =>
      (it.kind === "approval" || it.kind === "clarify") && it.requestId === requestId
        ? ({ ...it, answered: it.kind === "approval" ? answer : true } as ChatItem)
        : it,
    ),
  };
}

/** A stored transcript (session.resume / the sessions REST read) as chat items. */
export function fromTranscript(messages: Record<string, unknown>[], opts: { relaySession?: boolean } = {}): ChatItem[] {
  const out: ChatItem[] = [];
  for (const m of messages) {
    const role = String(m.role ?? "");
    const text = redactSecrets(typeof m.text === "string" ? m.text : typeof m.content === "string" ? m.content : "");
    if (role === "user") {
      // In a relay session the "user" turns are relay events: other agents' words.
      if (opts.relaySession) out.push({ kind: "relay", id: nextId("r"), text });
      else if (text) out.push({ kind: "user", id: nextId("u"), text });
    } else if (role === "assistant") {
      if (text) out.push({ kind: "agent", id: nextId("a"), text, streaming: false });
    } else if (role === "tool") {
      const name = String(m.name ?? "tool");
      const raw = typeof m.content === "string" ? m.content : str(m.content);
      let parsed: unknown = raw;
      try {
        parsed = JSON.parse(raw);
      } catch {
        /* plain text result */
      }
      const { text: output, failed, blocked } = toolOutput(parsed);
      out.push({
        kind: "tool",
        id: String(m.tool_call_id ?? nextId("t")),
        name,
        context: redactSecrets(String(m.context ?? "")),
        status: blocked ? "blocked" : failed ? "failed" : "done",
        output,
        relay: isRelayTool(name) || !!opts.relaySession,
      });
    }
  }
  return out;
}
