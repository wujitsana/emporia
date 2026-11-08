
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
