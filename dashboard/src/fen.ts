export const STARTING_FEN =
  "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

export function fenToBoard(fen: string): string[][] {
  return fen.split(" ")[0].split("/").map((rank) => {
    const row: string[] = [];
    for (const ch of rank) {
      const n = parseInt(ch);
      if (!isNaN(n)) for (let i = 0; i < n; i++) row.push("");
      else row.push(ch);
    }
    return row;
  });
}