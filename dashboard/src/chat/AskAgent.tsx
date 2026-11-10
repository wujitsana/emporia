import { useState } from "react";
import styles from "./AskAgent.module.css";
import { askAgent, chatAvailable } from "./chatAccess";
import { MODE } from "../transport";
import { standaloneQuestion } from "./standaloneQuestion";


/**
 * "Ask the agent about this": opens the chat with a question about what is on
 * screen already typed in. It fills text only; the person decides to send it.
 * In the standalone console there is no chat by design (chatAccess.ts), so the
 * button copies the same question to paste in the agent's own chat instead.
 * Draws nothing anywhere else.
 */
export function AskAgent({ prompt, label = "ask agent", block = false }: { prompt: string; label?: string; block?: boolean }) {
  const [copied, setCopied] = useState(false);
  if (!chatAvailable()) {
    if (MODE !== "console") return null;
    const copy = () => {
      const text = standaloneQuestion(prompt);
      const done = () => { setCopied(true); setTimeout(() => setCopied(false), 2500); };
      if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(done, done);
      else done();
    };
    const copyButton = (
      <span className={styles.copy}>
        <button type="button" className={styles.root} onClick={copy}
                title="Copies a ready-made question about what is on screen. Nothing is sent anywhere.">
          <span aria-hidden className={styles.glyph}>›_</span> {copied ? "copied" : "ask your agent"}
        </button>
        <span className={styles.note}>Paste it in your agent's own chat (Claude Desktop, Codex, …).</span>
      </span>
    );
    return block ? <div className={styles.row}>{copyButton}</div> : copyButton;
  }
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
