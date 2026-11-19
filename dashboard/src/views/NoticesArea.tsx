/**
 * The notices area at the top of Home, in Hermes only: what the plugin says needs the person (a newer plugin in the
 * Hermes catalog, the runtime behind the plugin, game moves that waited for the daily turn cap, a reply waiting
 * with no round scheduled, the relay's update note). The same fixed texts `hermes emporia notices status` prints
 * (plugin_api.py `/_notices`). Read-only: it names the command, it never runs one, and it draws nothing when
 * there is nothing to say.
 */
import { useEffect, useState } from "react";
import { consoleUrl, httpFetch, IN_HERMES } from "../transport";

export type Notice = { id: string; text: string };

/** Text outside backticks is plain; a `command` is set apart in code type. */
function Pieces({ text }: { text: string }) {
  return (
    <>
      {text.split("`").map((part, i) => (i % 2 ? <code key={i}>{part}</code> : <span key={i}>{part}</span>))}
    </>
  );
}

export function NoticesArea() {
  const [notices, setNotices] = useState<Notice[]>([]);
  useEffect(() => {
    if (!IN_HERMES) return undefined;
    let gone = false;
    httpFetch(consoleUrl("/_notices"))
      .then((r) => (r.ok ? r.json() : { notices: [] }))
      .then((d: { notices?: unknown }) => {
        if (gone || !Array.isArray(d?.notices)) return;
        setNotices(d.notices.filter((n): n is Notice => typeof n?.id === "string" && typeof n?.text === "string"));
      })
      .catch(() => undefined);
    return () => { gone = true; };
  }, []);
  if (!notices.length) return null;
  // The same block as First steps and the others on Home: framed card, label chip, one labelled row each.
  return (
    <div className="e-start">
      <section className="e-now" aria-label="Notices">
        <header className="e-now__head"><span>Notices</span><span>{notices.length}</span></header>
        {notices.map((n) => (
          <p key={n.id} className="e-now__row e-now__row--static" data-notice={n.id}>
            <span className="e-now__label">notice</span>
            <span><Pieces text={n.text} /></span>
            <span />
          </p>
        ))}
      </section>
    </div>
  );
}
