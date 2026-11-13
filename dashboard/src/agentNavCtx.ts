import { createContext, useContext } from "react";
import type { Navigate } from "./navigation";

/**
 * How an agent's name or glyph opens its page. App provides it once; every glyph (AgentMoireAvatar) and name
 * (ui/AgentName) reads it, so no view wires its own link. With no provider (a test, a bare component) they are
 * plain, inert drawings.
 */
export const AgentNavCtx = createContext<Navigate | null>(null);
export const useAgentNav = (): Navigate | null => useContext(AgentNavCtx);
