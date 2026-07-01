import type { SVGProps } from "react";

type SvgProps = SVGProps<SVGSVGElement>;

const base = {
  className: "e-chess-svg",
  viewBox: "0 0 16 16",
  width: 16,
  height: 16,
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.35,
  strokeLinecap: "square",
  strokeLinejoin: "miter",
  "aria-hidden": true as const,
};

/** Step back — line chevron (monochrome). */
export function IconStepPrev(props: SvgProps) {
  return (
    <svg {...base} {...props}>
      <path d="M10 2.5 5.5 8 10 13.5" />
    </svg>
  );
}

/** Step forward — line chevron. */
export function IconStepNext(props: SvgProps) {
  return (
    <svg {...base} {...props}>
      <path d="M6 2.5 10.5 8 6 13.5" />
    </svg>
  );
}

/** Traditional play — open triangle. */
export function IconPlay(props: SvgProps) {
  return (
    <svg {...base} {...props}>
      <path d="M5.5 3.5v9l7-4.5-7-4.5z" fill="currentColor" stroke="none" />
    </svg>
  );
}

/** Stop — square outline (SRCL / terminal media idiom). */
export function IconStop(props: SvgProps) {
  return (
    <svg {...base} {...props}>
      <rect x="4.5" y="4.5" width="7" height="7" />
    </svg>
  );
}

/** Jump to latest — circular refresh arrow (browser-style). */
export function IconRefreshLatest(props: SvgProps) {
  return (
    <svg {...base} {...props}>
      <path d="M8 2.5a5.5 5.5 0 0 1 4.8 2.8" />
      <path d="M12.8 2.2v3.1h-3.1" />
      <path d="M8 13.5a5.5 5.5 0 0 1-4.8-2.8" />
      <path d="M3.2 13.8v-3.1h3.1" />
    </svg>
  );
}