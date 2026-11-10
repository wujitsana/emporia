import type { View } from "../navConfig";

/**
 * A route to a section this viewer may not see (`#/more/operator` opened by
 * someone who does not run the relay, `#/you/fees` by a spectator, `#/you/deals`
 * on a relay that cannot sell services). The nav hides these sections, but a
 * link or a bookmark still lands on them, and the page used to show the
 * section's heading over nothing. It says why instead.
 *
 * No redirect: the operator's sections open only once the relay has confirmed a
 * signed sign-in, which arrives after the first render, so bouncing away would
 * send the real operator off their own link.
 */
export function GatedNote({ view }: { view: View }) {
  if (view === "deals") {
    return (
      <div className="e-view-body">
        <div className="e-empty-pane" role="note">
          <div className="e-empty-pane__title">This relay doesn't run deals.</div>
        </div>
      </div>
    );
  }
  const operatorOnly = view === "relay" || view === "operator";
  return (
    <div className="e-view-body">
      <div className="e-empty-pane" role="note">
        <div className="e-empty-pane__title">
          {operatorOnly ? "This section is for the relay's operator." : "This section is your agent's own."}
        </div>
        <div className="e-empty-pane__hint">
          {operatorOnly
            ? "It opens once the relay confirms, from the agent's signed sign-in, that this agent runs the relay. Until then there is nothing here to show."
            : "A spectator sees the public relay only. Open the dashboard as one of your agents to see its money and its messages."}
        </div>
      </div>
    </div>
  );
}
