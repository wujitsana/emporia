/**
 * The part of Hermes Desktop's plugin SDK this page uses (apps/desktop/src/sdk
 * in Hermes). At runtime the Desktop loader resolves `@hermes/plugin-sdk` to the
 * app's live SDK; nothing of it is bundled.
 */
declare module "@hermes/plugin-sdk" {
  import type { ComponentType, ReactNode } from "react";

  export interface ReadableAtom<T> {
    get(): T;
    subscribe(fn: (value: T) => void): () => void;
    listen(fn: (value: T) => void): () => void;
  }

  export interface PluginContribution {
    id: string;
    area: string;
    order?: number;
    title?: string;
    render?: () => ReactNode;
    data?: unknown;
  }

  export interface PluginNativeNotificationInput {
    title: string;
    body?: string;
    silent?: boolean;
    activate?: string | { path: string; params?: Record<string, string> };
    onActivate?: () => void;
  }

  export interface PluginSettingsPage {
    id: string;
    title: string;
    icon?: string;
    order?: number;
    render: () => ReactNode;
  }

  export interface PluginContext {
    readonly source: string;
    register: (c: PluginContribution) => () => void;
    registerMany: (cs: PluginContribution[]) => () => void;
    registerSettingsPage?: (page: PluginSettingsPage) => () => void;
    onDispose: (fn: () => void) => void;
    rest: <T>(path: string, opts?: { method?: string; body?: unknown; timeoutMs?: number }) => Promise<T>;
    socket: (path: string, onMessage: (data: unknown) => void) => () => void;
    os: { notify: (input: PluginNativeNotificationInput) => void };
    storage: {
      get<T>(key: string, fallback: T): T;
      set(key: string, value: unknown): void;
      remove(key: string): void;
    };
  }

  export interface HermesPlugin {
    id: string;
    name?: string;
    description?: string;
    defaultEnabled?: boolean;
    register: (ctx: PluginContext) => void;
  }

  export const host: {
    state: { profile: ReadableAtom<string> };
    navigate: (path: string) => void;
  };

  export const ROUTES_AREA: string;
  export const SIDEBAR_NAV_AREA: string;
  export const PALETTE_AREA: string;
  export const STATUSBAR_AREAS: { left: string; right: string };

  export function useValue<T>(atom: ReadableAtom<T>): T;

  export const Switch: ComponentType<{
    checked?: boolean;
    onCheckedChange?: (checked: boolean) => void;
    "aria-label"?: string;
    id?: string;
  }>;
}
