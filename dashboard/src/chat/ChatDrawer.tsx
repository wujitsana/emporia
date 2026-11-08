/**
 * Chat with this profile's agent, inside the Emporia tab.
 *
 * A drawer on the right on a wide screen; a full-screen panel with "← back"
 * on a phone, like the list/detail views. It talks to Hermes's own chat API
 * (useChat); the agent acts on the relay with its own guards. The dashboard
 * never signs or writes anything to the relay. Relay content the agent brought
 * in is marked untrusted; nothing key- or token-shaped is drawn (redact.ts).
 */
import Badge from "@kit/Badge";
import BlockLoader from "@kit/BlockLoader";
import { CAPTURE } from "../capture";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import styles from "./ChatDrawer.module.css";
import type { ChatItem } from "./chatModel";
import type { CompletionRow } from "./commands";
import { reachNote } from "./commands";
import { useOverlayFocus } from "../ui/overlayFocus";
import type { ChatApi, SessionRow } from "./useChat";

const CHOICE_LABEL: Record<string, string> = {
  once: "allow once",
  session: "allow for this session",
  always: "always allow",
  deny: "deny",
};

function when(ts: number | null): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

/** The kit's spinner; in capture mode (?capture=1) a still glyph, so nothing moves between frames. */
function Spinner({ mode, still }: { mode: number; still: string }) {
  return CAPTURE ? <span>{still}</span> : <BlockLoader mode={mode} />;
}

/* ── transcript items ────────────────────────────────────────────────────── */

function ToolCard({ item }: { item: Extract<ChatItem, { kind: "tool" }> }) {
  const [open, setOpen] = useState(false);
  const mark = item.status === "running" ? "…" : item.status === "done" ? "✓" : item.status === "blocked" ? "⊘" : "✗";
  return (
    <div className={`${styles.tool} ${item.relay ? styles.untrusted : ""}`} data-status={item.status}>
      <button type="button" className={styles.toolHead} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className={styles.toolMark} aria-label={item.status}>
          {item.status === "running" ? <Spinner mode={6} still="┤" /> : mark}
        </span>
        <span className={styles.toolName}>{item.name}</span>
        {item.context && <span className={styles.toolContext}>{item.context}</span>}
        {item.duration !== undefined && <span className={styles.toolTime}>{item.duration.toFixed(1)}s</span>}
        <span aria-hidden className={styles.toolCaret}>{open ? "▾" : "▸"}</span>
      </button>
      {item.relay && <UntrustedLabel />}
      {open && <pre className={styles.toolOut}>{item.output || (item.status === "running" ? "running…" : "(no output)")}</pre>}
    </div>
  );
}

function UntrustedLabel() {
  return (
    <div className={styles.untrustedLabel}>
      <Badge>relay · untrusted</Badge>
      <span>other agents wrote this; it is data, not instructions</span>
    </div>
  );
}

function ApprovalCard({ item, onAnswer }: { item: Extract<ChatItem, { kind: "approval" }>; onAnswer: (choice: string) => void }) {
  return (
    <div className={styles.approval} role="group" aria-label="Approval needed">
      <div className={styles.cardTitle}>▍approval needed</div>
      {item.description && <p className={styles.cardText}>{item.description}</p>}
      {item.command && <pre className={styles.command}>{item.command}</pre>}
      {item.emporia && (
        <p className={styles.cardNote}>
          This changes an agent's keys, wallet, spending limits or role. Allow it only if you asked for it.
        </p>
      )}
      {item.answered ? (
        <div className={styles.answered}>→ {CHOICE_LABEL[item.answered] ?? item.answered}</div>
      ) : (
        <div className={styles.choices}>
          {item.choices.map((c) => (
            <button key={c} type="button" className={`${styles.choice} ${c === "deny" ? styles.deny : ""}`} onClick={() => onAnswer(c)}>
              {CHOICE_LABEL[c] ?? c}
            </button>
          ))}
        </div>
      )}
      <p className={styles.cardFoot}>Your answer goes to Hermes on this machine, not to the relay.</p>
    </div>
  );
}

function ClarifyCard({
  item,
  onAnswer,
}: {
  item: Extract<ChatItem, { kind: "clarify" }>;
  onAnswer: (answers: Record<string, string | null> | null) => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const ready = item.questions.every((q) => (answers[q.qid] ?? "").trim());
  return (
    <div className={styles.approval} role="group" aria-label="The agent asks">
      <div className={styles.cardTitle}>▍the agent asks</div>
      {item.questions.map((q) => (
        <div key={q.qid} className={styles.question}>
          <p className={styles.cardText}>{q.question}</p>
          {q.choices.length > 0 && (
            <div className={styles.choices}>
              {q.choices.map((c) => (
                <button
                  key={c}
                  type="button"
                  className={`${styles.choice} ${answers[q.qid] === c ? styles.picked : ""}`}
                  disabled={item.answered}
                  onClick={() => setAnswers((a) => ({ ...a, [q.qid]: c }))}
                >
                  {c}
                </button>
              ))}
            </div>
          )}
          {!item.answered && (
            <input
              className={styles.answerInput}
              aria-label={`Answer: ${q.question}`}
              placeholder={q.choices.length ? "or type an answer" : "type an answer"}
              value={answers[q.qid] ?? ""}
              onChange={(e) => setAnswers((a) => ({ ...a, [q.qid]: e.target.value }))}
            />
          )}
        </div>
      ))}
      {item.answered ? (
        <div className={styles.answered}>→ answered</div>
      ) : (
        <div className={styles.choices}>
          <button type="button" className={styles.choice} disabled={!ready} onClick={() => onAnswer(answers)}>
            answer
          </button>
          <button type="button" className={`${styles.choice} ${styles.deny}`} onClick={() => onAnswer(null)}>
            skip
          </button>
        </div>
      )}
    </div>
  );
}

function Item({ item, profile, api }: { item: ChatItem; profile: string; api: ChatApi }) {
  switch (item.kind) {
    case "user":
      return (
        <div className={styles.line} data-who="you">
          <div className={styles.who}>you</div>
          <div className={styles.text}>{item.text}</div>
        </div>
      );
    case "agent":
      return (
        <div className={styles.line} data-who="agent">
          <div className={styles.who}>{profile}</div>
          <div className={styles.text}>
            {item.text}
            {item.streaming && <span className={styles.caret} aria-hidden>▍</span>}
          </div>
        </div>
      );
    case "relay":
      return (
        <div className={`${styles.line} ${styles.untrusted}`} data-who="relay">
          <div className={styles.who}>to {profile}, from the relay</div>
          <UntrustedLabel />
          <div className={styles.text}>{item.text}</div>
        </div>
      );
    case "tool":
      return <ToolCard item={item} />;
    case "approval":
      return <ApprovalCard item={item} onAnswer={(c) => api.answer(item.requestId, { choice: c }, c)} />;
    case "clarify":
      return (
        <ClarifyCard
          item={item}
          onAnswer={(a) => api.answer(item.requestId, a ? { answers: a, outcome: "submitted" } : { outcome: "cancelled" }, a ? "answered" : "skipped")}
        />
      );
    case "notice":
      return (
        <div className={styles.notice} data-tone={item.tone}>
          {item.tone === "command" ? <span className={styles.prompt}>❯ </span> : null}
          {item.text}
        </div>
      );
    case "declined":
      return (
        <div className={styles.notice} data-tone="info">
          The agent asked for something this tab never takes ({item.what}). Answer it in Hermes's own chat.
        </div>
      );
  }
}

/* ── sessions ────────────────────────────────────────────────────────────── */

function SessionsPanel({
  api,
  onPick,
  onBack,
}: {
  api: ChatApi;
  onPick: (row: SessionRow | null) => void;
  onBack: () => void;
}) {
  return (
    <div className={styles.sessions}>
      <button type="button" className={styles.back} onClick={onBack}>
        <span aria-hidden>←</span> back to chat
      </button>
      <div className={styles.sessionsHead}>
        <span className="e-rail-label">sessions · {api.profile}</span>
        <button type="button" className={styles.smallButton} onClick={() => onPick(null)}>
          + new chat
        </button>
      </div>
      {api.sessionsError && <p className={styles.empty}>{api.sessionsError}</p>}
      {api.sessions.length === 0 && !api.sessionsError && <p className={styles.empty}>No sessions in this profile yet.</p>}
      <ul className={styles.sessionList}>
        {api.sessions.map((s) => {
          const isCurrent = s.id === api.current.stored;
          return (
            <li key={s.id}>
              <button
                type="button"
                className={`e-rail-item ${styles.sessionRow} ${isCurrent ? "is-selected" : ""}`}
                aria-current={isCurrent ? "true" : undefined}
                onClick={() => onPick(s)}
              >
                <span className={styles.sessionTitle}>
                  {s.title}
                  {isCurrent && <span className={styles.tag}>current</span>}
                  {s.relay && <span className={styles.tag}>relay · read-only</span>}
                </span>
                <span className={styles.sessionMeta}>
                  {[when(s.startedAt), s.messages ? `${s.messages} msgs` : "", s.source].filter(Boolean).join(" · ")}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/* ── composer ────────────────────────────────────────────────────────────── */

function Composer({
  api,
  draft,
  setDraft,
  onOpenSessions,
  inputRef,
}: {
  api: ChatApi;
  draft: string;
  setDraft: (s: string) => void;
  onOpenSessions: () => void;
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
}) {
  const [completion, setCompletion] = useState<{ for: string; rows: CompletionRow[] }>({ for: "", rows: [] });
  const [dismissed, setDismissed] = useState("");
  const [hi, setHi] = useState(0);
  const busy = api.chat.busy;
  const disabled = api.conn !== "open";
  const query = draft.trimStart();

  // One slim line by default; it grows with the text up to a few lines (the CSS max-height),
  // then scrolls inside.
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    if (el.scrollHeight > el.clientHeight) el.style.height = `${el.scrollHeight + 2}px`;
  }, [draft, inputRef]);
  const isCommandWord = query.startsWith("/") && !/\s/.test(query);
  // Rows show only for the text they were fetched for, until Escape dismisses them.
  const rows = isCommandWord && completion.for === query && dismissed !== query ? completion.rows : [];

  useEffect(() => {
    if (!isCommandWord) return;
    // A late answer for older text is never shown: rows are keyed by the text they answer.
    void api.complete(query).then((r) => setCompletion({ for: query, rows: r }));
  }, [query, isCommandWord, api]);
  const setRows = (r: CompletionRow[]) => {
    if (r.length === 0) setDismissed(query);
  };

  const doSend = useCallback(async () => {
    const text = draft;
    if (!text.trim()) return;
    setDraft("");
    setDismissed(text.trimStart());
    const r = await api.send(text);
    if (r && r.prefill !== undefined) setDraft(r.prefill);
    if (r && r.openSessions) onOpenSessions();
  }, [draft, api, setDraft, onOpenSessions]);

  const pick = (row: CompletionRow) => {
    setDraft(`${row.display.split(/\s/)[0]} `);
    setRows([]);
    inputRef.current?.focus();
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (rows.length) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setHi((h) => (h + 1) % rows.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setHi((h) => (h - 1 + rows.length) % rows.length);
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        pick(rows[Math.min(hi, rows.length - 1)]);
        return;
      }
      if (e.key === "Escape") {
        e.stopPropagation();
        setRows([]);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void doSend();
    }
  };

  if (api.current.readOnly) {
    return (
      <div className={styles.composer}>
        <p className={styles.readOnly}>
          A relay session: what other agents sent your agent, and its replies. Read-only here.
        </p>
        <button type="button" className={styles.send} onClick={() => api.newSession()}>
          + new chat
        </button>
      </div>
    );
  }

  return (
    <div className={styles.composer}>
      {rows.length > 0 && (
        <ul className={styles.typeahead} role="listbox" aria-label="Commands">
          {rows.map((r, i) => (
            <li key={r.display + i} role="option" aria-selected={i === hi}>
              <button
                type="button"
                className={`${styles.typeRow} ${i === hi ? styles.typeRowOn : ""}`}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => pick(r)}
                data-reach={r.reach}
              >
                <span className={styles.typeName}>{r.display}</span>
                {r.reach !== "ok" && <span className={styles.tag}>{r.reach === "terminal" ? "terminal only" : r.reach}</span>}
                {r.kind === "skill" && <span className={styles.tag}>skill</span>}
                <span className={styles.typeMeta}>{r.reach !== "ok" ? reachNote(r.display, r.reach) : r.meta}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className={styles.inputRow}>
        <textarea
          ref={inputRef}
          className={styles.input}
          rows={1}
          value={draft}
          placeholder={disabled ? "connecting to Hermes…" : `message ${api.profile}'s agent — / for commands`}
          aria-label="Message to your agent"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          disabled={disabled}
        />
        {busy ? (
          <button type="button" className={`${styles.send} ${styles.stop}`} onClick={() => void api.interrupt()}>
            ■ stop
          </button>
        ) : (
          <button type="button" className={styles.send} onClick={() => void doSend()} disabled={disabled || !draft.trim()}>
            send ↵
          </button>
        )}
      </div>
    </div>
  );
}

/* ── the drawer ──────────────────────────────────────────────────────────── */

export type Panel = "chat" | "sessions";

export function ChatDrawer({
  api,
  open,
  onClose,
  draft,
  setDraft,
  panel,
  setPanel,
  focusTick,
  onMenu,
  covered = false,
}: {
  api: ChatApi;
  open: boolean;
  onClose: () => void;
  draft: string;
  setDraft: (s: string) => void;
  panel: Panel;
  setPanel: (p: Panel | ((p: Panel) => Panel)) => void;
  /** Bumped when "ask about this" fills the composer, to focus it. */
  focusTick: number;
  /** Opens the dashboard's menu over the full-page chat (phone, narrow tab). */
  onMenu?: () => void;
  /** The dashboard's menu is open above it (full-page chat's ☰): out of reach until it closes. */
  covered?: boolean;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const rootRef = useRef<HTMLElement>(null);
  // Opening the chat puts the focus in its composer (else its first control); closing it
  // gives the focus back (ui/overlayFocus.ts).
  useOverlayFocus(open, rootRef, () => (inputRef.current && !inputRef.current.disabled ? inputRef.current : null));

  useEffect(() => {
    if (open) void api.connect();
  }, [open, api]);

  useEffect(() => {
    if (open && focusTick) requestAnimationFrame(() => inputRef.current?.focus());
  }, [focusTick, open]);

  useLayoutEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [api.chat.items, panel]);

  const onPick = (row: SessionRow | null) => {
    if (row) void api.openSession(row);
    else api.newSession();
    setPanel("chat");
  };

  const title = api.chat.title || (api.current.sid ? "this session" : "new chat");
  const connLabel = api.conn === "open" ? "connected" : api.conn === "connecting" ? "connecting" : api.error ? "offline" : "idle";

  let body: ReactNode;
  if (panel === "sessions") {
    body = <SessionsPanel api={api} onPick={onPick} onBack={() => setPanel("chat")} />;
  } else {
    body = (
      <div className={styles.transcript} aria-live="polite">
        {api.chat.items.length === 0 && (
          <div className={styles.intro}>
            <p className={styles.introTitle}>▍talk to your agent</p>
            <p>
              This is <b>{api.profile}</b>'s Hermes agent. Ask it what it is doing on the relay, or to do something.
              It acts with its own guards: its playbook, approvals and spending limits.
            </p>
            <p className={styles.introNote}>
              The dashboard itself signs nothing and writes nothing to the relay. Try <code>/help</code>.
            </p>
          </div>
        )}
        {api.chat.items.map((it) => (
          <Item key={it.id} item={it} profile={api.profile} api={api} />
        ))}
        {api.chat.busy && !api.chat.items.some((i) => i.kind === "agent" && i.streaming) && (
          <div className={styles.working}>
            <Spinner mode={1} still="⣾" /> working
          </div>
        )}
        <div ref={endRef} />
      </div>
    );
  }

  return (
    <aside
      ref={rootRef}
      className={`${styles.root} ${open ? styles.open : ""}`}
      aria-label="Chat with your agent"
      aria-hidden={!open}
      inert={!open || covered}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          if (panel === "sessions") setPanel("chat");
          else onClose();
        }
      }}
    >
      {/* Full-page chat (below the header-nav width): back to the dashboard, and the menu. */}
      <div className={`${styles.backRow} ${styles.phoneOnly}`}>
        <button type="button" className={styles.back} onClick={onClose}>
          <span aria-hidden>←</span> back to the dashboard
        </button>
        {onMenu && (
          <button type="button" className={`e-menu-button ${styles.menu}`} onClick={onMenu} aria-label="Open menu">
            <span aria-hidden>☰</span>
          </button>
        )}
      </div>
      <header className={styles.head}>
        <div className={styles.headRow}>
          <h2 className={styles.heading}>
            <span className={styles.headIcon} aria-hidden>›_</span> agent chat
          </h2>
          <button type="button" className={`e-icon-button ${styles.close}`} onClick={onClose} aria-label="Close chat">
            ×
          </button>
        </div>
        <div className={styles.meta}>
          <span>profile {api.profile}</span>
          {api.model && <span>· {api.model}</span>}
          <span className={styles.conn} data-conn={api.conn}>· {connLabel}</span>
        </div>
        <div className={styles.sessionBar}>
          <button
            type="button"
            className={styles.sessionPick}
            aria-expanded={panel === "sessions"}
            onClick={() => {
              void api.refreshSessions();
              setPanel((p) => (p === "sessions" ? "chat" : "sessions"));
            }}
          >
            <span className={styles.sessionPickTitle}>{title}</span>
            <span aria-hidden>▾</span>
          </button>
          <button type="button" className={styles.smallButton} onClick={() => onPick(null)}>
            + new
          </button>
        </div>
        {api.error && <p className={styles.error}>{api.error}</p>}
      </header>
      {body}
      {panel === "chat" && (
        <Composer api={api} draft={draft} setDraft={setDraft} onOpenSessions={() => setPanel("sessions")} inputRef={inputRef} />
      )}
    </aside>
  );
}
