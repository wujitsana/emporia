import type { ReactNode } from "react";

/** Flat option chips — selected = accent border + text, no gray/secondary fill. */
export function SegmentTabs<T extends string>({
  options,
  value,
  onChange,
  labels,
  className,
}: {
  options: readonly T[];
  value: T;
  onChange: (v: T) => void;
  labels?: Partial<Record<T, string>>;
  className?: string;
}) {
  return (
    <div className={["e-segment-tabs", className].filter(Boolean).join(" ")} role="tablist">
      {options.map((o) => {
        const active = value === o;
        return (
          <button
            key={o}
            type="button"
            role="tab"
            aria-selected={active}
            className={`e-segment-tab${active ? " is-active" : ""}`}
            onClick={() => onChange(o)}
          >
            {labels?.[o] ?? o}
          </button>
        );
      })}
    </div>
  );
}

/** Thin sub-view header (replaces gray SRCL Navigation bar chrome). */
export function SubHeader({ left, right }: { left?: ReactNode; right?: ReactNode }) {
  if (!left && !right) return null;
  return (
    <div className="e-sub-header">
      <div className="e-sub-header__left">{left}</div>
      <div className="e-sub-header__right">{right}</div>
    </div>
  );
}