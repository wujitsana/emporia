// Pixel badges: import from here. The ui-badges design note (kept outside the repo) says where each one belongs.
// `PixelBadge` is the same component, for files that already import the kit's text `Badge`.
export { Badge, Badge as PixelBadge, type BadgeProps } from "./Badge";
export { BADGES, fallbackBadge, galleryIds, normalizeId, resolveBadge, type BadgeDef, type BadgeKind } from "./catalog.ts";
