import styles from "./AskAgent.module.css";
import { askAgent, chatAvailable } from "./chatAccess";

/**
 * "Ask the agent about this": opens the chat with a question about what is on
 * screen already typed in. It fills text only; the person decides to send it.
 * Draws nothing where there is no chat (outside the Hermes tab).
 */
export function AskAgent({ prompt, label = "ask agent", block = false }: { prompt: string; label?: string; block?: boolean }) {
  if (!chatAvailable()) return null;
  const button = (
    <button
      type="button"
      className={styles.root}
      onClick={() => askAgent(prompt)}
      title="Opens the chat with this question typed in. Nothing is sent until you send it."
    >
      <span aria-hidden className={styles.glyph}>›_</span> {label}
    </button>
  );
  return block ? <div className={styles.row}>{button}</div> : button;
}
