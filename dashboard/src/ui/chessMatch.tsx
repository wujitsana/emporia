import type { Session } from "../api";
import { chessSides, shortAgent } from "../chessReplay";

export function ChessPlayersBar({ session }: { session: Session }) {
  const { white, black } = chessSides(session);
  return (
    <div className="e-chess-players" aria-label="Players">
      <div className="e-chess-players__side" aria-label={`White: ${white}`}>
        <span className="e-chess-players__piece" aria-hidden>
          ♔
        </span>
        <span className="e-chess-players__name">{shortAgent(white, 22)}</span>
      </div>
      <span className="e-chess-players__vs" aria-hidden>
        vs
      </span>
      <div className="e-chess-players__side" aria-label={`Black: ${black}`}>
        <span className="e-chess-players__piece e-chess-players__piece--dark" aria-hidden>
          ♚
        </span>
        <span className="e-chess-players__name">{shortAgent(black, 22)}</span>
      </div>
    </div>
  );
}

export function ChessMoveLine({
  sans,
  idx,
  onPick,
}: {
  sans: string[];
  idx: number;
  onPick: (frameIndex: number) => void;
}) {
  if (sans.length === 0) return null;
  return (
    <div className="e-chess-movelist" role="list">
      {sans.map((san, i) => {
        const frame = i + 1;
        const moveNo = Math.floor(i / 2) + 1;
        const prefix = i % 2 === 0 ? `${moveNo}.` : "";
        const active = frame === idx;
        return (
          <button
            key={`${moveNo}-${i}-${san}`}
            type="button"
            role="listitem"
            className={`e-chess-movelist__btn${active ? " e-chess-movelist__btn--on" : ""}`}
            onClick={() => onPick(frame)}
            title={`Go to after ${san}`}
          >
            {prefix}
            {san}
          </button>
        );
      })}
    </div>
  );
}