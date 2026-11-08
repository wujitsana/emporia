/**
 * Where the chat plugs into the app: a toggle for the header and the drawer
 * for the shell. Both draw nothing outside the Hermes tab (chatAccess.ts).
 */
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import styles from "./AgentChat.module.css";
import { ChatDrawer, type Panel } from "./ChatDrawer";
import { chatAvailable, chatFetch, chatOpen, makeGateway, onAsk, setChatOpen, subscribeChatOpen } from "./chatAccess";
import { useChat } from "./useChat";

function useOpen(): boolean {
  return useSyncExternalStore(subscribeChatOpen, chatOpen);
}

/** The header button: `›_ chat`. */
export function ChatToggle({ compact = false }: { compact?: boolean }) {
  const isOpen = useOpen();
  if (!chatAvailable()) return null;
  return (
    <button
      type="button"
      className={`e-icon-button ${styles.toggle} ${isOpen ? styles.on : ""}`}
      aria-expanded={isOpen}
      aria-label={isOpen ? "Close agent chat" : "Open agent chat"}
      onClick={() => setChatOpen(!isOpen)}
    >
      <span aria-hidden className={styles.glyph}>›_</span>
      {!compact && <span className={styles.label}>chat</span>}
    </button>
  );
}

/** The first row of the ☰ menu: the chat is a companion, not a section, so it sits above the groups. */
export function ChatMenuItem({ onOpen }: { onOpen: () => void }) {
  if (!chatAvailable()) return null;
  return (
    <div className={styles.menuItem}>
      <button
        type="button"
        className="e-section-tab"
        onClick={() => {
          onOpen();
          setChatOpen(true);
        }}
      >
        <span className={`e-section-tab__icon ${styles.glyph}`} aria-hidden>›_</span>
        <span className="e-section-tab__label">Chat with your agent</span>
      </button>
    </div>
  );
}

/** The drawer, mounted once at the shell level. */
export function AgentChatDrawer({ onMenu, covered = false }: { onMenu?: () => void; covered?: boolean } = {}) {
  if (!chatAvailable()) return null;
  return <DrawerInner onMenu={onMenu} covered={covered} />;
}

function DrawerInner({ onMenu, covered }: { onMenu?: () => void; covered: boolean }) {
  const isOpen = useOpen();
  const gw = useMemo(() => makeGateway(), []);
  const api = useChat(gw, chatFetch);
  const [draft, setDraft] = useState("");
  const [panel, setPanel] = useState<Panel>("chat");
  const [focusTick, setFocusTick] = useState(0);

  useEffect(() => () => gw?.close(), [gw]);
  useEffect(
    () =>
      onAsk((text) => {
        setDraft(text);
        setPanel("chat");
        setChatOpen(true);
        setFocusTick((n) => n + 1);
      }),
    [],
  );

  return (
    <ChatDrawer
      api={api}
      open={isOpen}
      onClose={() => setChatOpen(false)}
      draft={draft}
      setDraft={setDraft}
      panel={panel}
      setPanel={setPanel}
      focusTick={focusTick}
      onMenu={onMenu}
      covered={covered}
    />
  );
}
