import { defineConfig } from "vitest/config";
import path from "path";
import { cssModuleName } from "./src/cssModuleName";

const KIT = path.resolve("src/ui/kit");

// P21-6: closes the JS-side half of D2 ("nothing pins a Python protocol
// constant to its TypeScript mirror") -- the Python-reads-api.ts text pin
// (tests/test_emporia.py) stays as the cross-language check; this is the
// dashboard actually running its own logic under test, which it never did
// before this pass. Mirrors vite.config.ts's aliases so a future test that
// touches a view component (not just api.ts, which needs none of them)
// doesn't need its own separate resolution setup.
export default defineConfig({
  resolve: {
    alias: {
      "@kit": KIT,
    },
  },
  css: { modules: { localsConvention: "camelCase", generateScopedName: cssModuleName } },
  // P171: the Docs view bundles ../README.md, ../SECURITY.md, ../docs/*.md.
  server: { fs: { allow: [path.resolve("..")] } },
  test: {
    environment: "node",
  },
});
