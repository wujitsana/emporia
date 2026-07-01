import type { ReactNode } from "react";
import {
  IconPlay,
  IconRefreshLatest,
  IconStepNext,
  IconStepPrev,
  IconStop,
} from "./chessIcons";

type ChessTransportProps = {
  onPrev: () => void;
  onNext: () => void;
  onReplayToggle: () => void;
  replaying: boolean;
  onLatest: () => void;
  /** Live / move counter — sits on the transport row (no extra view header). */
  status?: ReactNode;
};

function IconBtn({
  icon,
  label,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button type="button" className="e-chess-icon-btn" onClick={onClick} aria-label={label} title={label}>
      <span className="e-chess-icon-btn__glyph">{icon}</span>
    </button>
  );
}

export function ChessTransport({ onPrev, onNext, onReplayToggle, replaying, onLatest, status }: ChessTransportProps) {
  return (
    <div className="e-chess-ctrl-row" role="toolbar" aria-label="Chess replay">
      <div className="e-chess-ctrl-icons">
        <IconBtn icon={<IconStepPrev />} label="Previous move" onClick={onPrev} />
        <IconBtn icon={<IconStepNext />} label="Next move" onClick={onNext} />
        <IconBtn
          icon={replaying ? <IconStop /> : <IconPlay />}
          label={replaying ? "Stop replay" : "Play replay"}
          onClick={onReplayToggle}
        />
        <IconBtn icon={<IconRefreshLatest />} label="Jump to latest" onClick={onLatest} />
      </div>
      {status ? <div className="e-chess-ctrl-status">{status}</div> : null}
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
    <div className="e-chess-speed">
      <span className="e-chess-speed__lbl">speed</span>
      <input
        type="range"
        min={200}
        max={2000}
        step={100}
        value={speed}
        onChange={(e) => onSpeed(Number(e.target.value))}
        className="e-chess-speed__range"
      />
      <span className="e-chess-speed__lbl">{speed}ms/move</span>
    </div>
  );
}

export function LiveMark({ live }: { live?: boolean }) {
  if (!live) return null;
  return <span className="e-chess-live-mark">live</span>;
}

export function MoveIndex({ idx, total }: { idx: number; total: number }) {
  return (
    <span className="e-chess-move-idx">
      {idx + 1}/{total}
    </span>
  );
}

export function ChessReplayStatus({
  live,
  idx,
  total,
}: {
  live?: boolean;
  idx: number;
  total: number;
}) {
  return (
    <span className="e-chess-replay-status">
      {live ? <span className="e-chess-live-mark">live</span> : null}
      <MoveIndex idx={idx} total={total} />
    </span>
  );
}