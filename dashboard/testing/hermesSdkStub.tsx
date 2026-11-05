/**
 * A stand-in for Hermes Desktop's `@hermes/plugin-sdk`, for the vitest run only
 * (vitest.config.ts aliases the specifier here). The real SDK is the Desktop
 * app's own; the page never bundles it.
 */
import { useSyncExternalStore } from "react";

type Listener<T> = (v: T) => void;

export function atom<T>(initial: T) {
  let value = initial;
  const listeners = new Set<Listener<T>>();
  return {
    get: () => value,
    set: (v: T) => {
      value = v;
      listeners.forEach((fn) => fn(v));
    },
    listen: (fn: Listener<T>) => {
      listeners.add(fn);
      return () => { listeners.delete(fn); };
    },
    subscribe: (fn: Listener<T>) => {
      listeners.add(fn);
      fn(value);
      return () => { listeners.delete(fn); };
    },
  };
}

export const profileAtom = atom("alpha");
export const navigated: string[] = [];

export const host = {
  state: { profile: profileAtom },
  navigate: (path: string) => { navigated.push(path); },
};

export function useValue<T>(a: { get: () => T; listen: (fn: Listener<T>) => () => void }): T {
  return useSyncExternalStore((cb) => a.listen(() => cb()), a.get, a.get);
}

export const ROUTES_AREA = "routes";
export const SIDEBAR_NAV_AREA = "sidebar.nav";
export const PALETTE_AREA = "palette";
export const STATUSBAR_AREAS = { left: "statusBar.left", right: "statusBar.right" };

export function Switch(props: { checked?: boolean; onCheckedChange?: (v: boolean) => void; "aria-label"?: string }) {
  return (
    <input type="checkbox" role="switch" aria-label={props["aria-label"]} checked={!!props.checked}
      onChange={(e) => props.onCheckedChange?.(e.target.checked)} />
  );
}
