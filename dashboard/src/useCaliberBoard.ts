import { useInterval } from "./hooks";
import { api } from "./api";
import { isModuleOff, type CaliberBoardData } from "./caliberFormat";

/** Reads the Caliber board once for the Arena's Boards page: `off` when the relay does not run the module. */
export function useCaliberBoard(): { data: CaliberBoardData | null; off: boolean; error: string | null } {
  const [data, , error] = useInterval(() => api.caliberBoard(), 60_000, []);
  return { data, off: !data && isModuleOff(error), error: data ? null : error };
}
