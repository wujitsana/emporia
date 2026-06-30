import type { ReactNode } from "react";

type ChessTransportProps = {
  onPrev: () => void;
  onNext: () => void;
  onReplayToggle: () => void;
  replaying: boolean;
  onLatest: () => void;
  latestLabel?: string;
};

/** Flat chess transport — no SRCL ActionButton gray/secondary fills. */
export function ChessTransport({
  onPrev,
  onNext,
  onReplayToggle,
  replaying,
  onLatest,
  latestLabel = "live",
}: ChessTransportProps) {
  return (
    <div className="e-chess-ctrl-row">
      <button type="button" className="e-ctrl-btn" onClick={onPrev}>
        <kbd>←</kbd> prev
      </button>
      <button type="button" className="e-ctrl-btn" onClick={onNext}>
        <kbd>→</kbd> next
      </button>
      <button type="button" className="e-ctrl-btn" onClick={onReplayToggle}>
        <kbd>R</kbd> {replaying ? "stop" : "replay"}
      </button>
      <button type="button" className="e-ctrl-btn" onClick={onLatest}>
        <kbd>L</kbd> {latestLabel}
      </button>
    </div>
  );
}

export function ChessSpeedSlider({
  speed,
  onSpeed,
}: {
  speed: number;
  onSpeed: (ms: number) => void;
}) {
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <span className="e-dim" style={{ fontSize: "0.68rem" }}>
        speed
      </span>
      <input
        type="range"
        min={200}
        max={2000}
        step={100}
        value={speed}
        onChange={(e) => onSpeed(Number(e.target.value))}
        style={{ width: 100, accentColor: "var(--theme-focused-foreground)" }}
      />
      <span className="e-dim" style={{ fontSize: "0.68rem" }}>
        {speed}ms/move
      </span>
    </div>
  );
}

export function LiveMark({ live }: { live?: boolean }) {
  if (!live) return null;
  return <span className="e-dim e-status-txt">live</span>;
}

export function MoveIndex({ idx, total }: { idx: number; total: number }) {
  return (
    <span className="e-dim" style={{ fontSize: "0.68rem" }}>
      move {idx + 1}/{total}
    </span>
  );
}