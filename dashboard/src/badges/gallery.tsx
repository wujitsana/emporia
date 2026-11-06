// The badge gallery's own entry (dashboard/badges.html): `npm run dev`, then open /badges.html.
// A dev page only: no build config lists it, so no plugin, console or Desktop bundle carries it.
import "../ui/kit/global.css";
import "../dashboard.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { BadgeGallery } from "./BadgeGallery";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BadgeGallery />
  </StrictMode>,
);
