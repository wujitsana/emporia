/**
 * The plugin's version, compiled in by the plugin builds from plugin.yaml
 * (buildMeta.ts), so the tab and the Desktop page always say the same one.
 * "dev" in the dev server and the tests.
 */
declare const __EMPORIA_VERSION__: string | undefined;

export const EMPORIA_VERSION: string = typeof __EMPORIA_VERSION__ === "string" ? __EMPORIA_VERSION__ : "dev";
