import { arrowKeys } from "../arrowKeys";

/**
 * Underline tabs inside a page, the look of the sub-row under the header: the active one marked by its rule, the
 * row scrolling sideways on a narrow screen (never the page). Arrow keys move between the tabs.
 */
export function UnderlineTabs<T extends string>({ items, value, onChange, label }: {
  items: { id: T; label: string; count?: number }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  return (
    <div className="e-utabs" role="tablist" aria-label={label} onKeyDown={arrowKeys}>
      {items.map((t) => (
        <button key={t.id} type="button" role="tab" aria-selected={t.id === value}
          className={`e-subnav__tab e-utabs__tab${t.id === value ? " is-active" : ""}`} onClick={() => onChange(t.id)}>
          {t.label}{t.count ? <span className="e-faint e-utabs__count">{t.count}</span> : null}
        </button>
      ))}
    </div>
  );
}
