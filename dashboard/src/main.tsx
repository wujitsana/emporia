import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ensureViewer } from "./viewerBootstrap";

// P126: served by the local console, the viewer is chosen before the app (and
// relayEnv's VIEWER_ID) loads; served by a relay, identity is build-time.
const viaConsole = import.meta.env.VITE_EMPORIA_MODE === "console";

(viaConsole
  ? ensureViewer(() => fetch("/agents", { credentials: "same-origin" })
      .then((r) => r.json()).then((d) => d.agents as string[]))
  : Promise.resolve()
).then(async () => {
  const { default: App } = await import("./App.tsx");
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>
  );
});
