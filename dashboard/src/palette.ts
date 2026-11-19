/**
 * The one palette: each tint with a light and a deep companion, used by the themes (the dark theme's accent is the light
 * companion, the light theme's accent the deep one; dark green is pinned to the video's #39ff44), the agent pictures (their
 * shades and accents) and the chat sender names. A test holds the CSS, the theme model and the pictures to this table.
 *
 *   tint            light       deep
 *   green  #39ff44  #72e072     #006c00
 *   blue   #0047ff  #69cdff     #005e8b
 *   crimson #b00331 #ffbeff     #743c76
 *   burnt orange #d57c22  #ffad33  #814400
 *   purple #7d3c98  #dc97fb     #74338e
 *   charcoal #888888  #b7b7b7   #555555
 */
import type { Tint } from "./themeConfig.ts";

export interface Companions { light: string; deep: string }

export const COMPANIONS: Record<Tint, Companions> = {
  "tint-green": { light: "#72e072", deep: "#006c00" },
  "tint-blue": { light: "#69cdff", deep: "#005e8b" },
  "tint-red": { light: "#ffbeff", deep: "#743c76" },
  "tint-orange": { light: "#ffad33", deep: "#814400" },
  "tint-purple": { light: "#dc97fb", deep: "#74338e" },
  "tint-charcoal": { light: "#b7b7b7", deep: "#555555" },
};
