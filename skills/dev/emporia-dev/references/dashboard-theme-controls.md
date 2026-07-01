# Dashboard theme controls — implementation notes

Session pattern: **square framed duo** (mode | accent), not stranded circles or rounded pill.

## Files

| File | Role |
|------|------|
| `dashboard/src/themeConfig.ts` | `toggleTheme`, `nextTint`, `applyTheme`, `TINTS`, `TINT_COLORS` |
| `dashboard/src/ThemeControls.tsx` | `ThemePanelBody` → `.e-theme-duo` + rule + two `.e-theme-circle` buttons |
| `dashboard/src/dashboard.css` | `.e-theme-duo`, `.e-theme-duo__rule`, `.e-theme-circle*` |
| `dashboard/src/App.tsx` | `ThemeCompact` in desktop + mobile top bars |
| `dashboard/src/AppNav.tsx` | `ThemePanelBody` in mobile drawer |
| `dashboard/src/SocialHubView.tsx` | Profile **APPEARANCE** → `ThemeProfileCard` |

## Markup shape

```tsx
<div className="e-theme-duo">
  <button className="e-theme-circle e-theme-circle--mode e-theme-duo__btn" data-mode={theme} />
  <span className="e-theme-duo__rule" aria-hidden />
  <button className="e-theme-circle e-theme-circle--accent e-theme-duo__btn" style={{ background: TINT_COLORS[tint] }} />
</div>
```

## CSS (operator preference)

- **Outer frame:** `border-radius: 0` (square), thin `1px` border, horizontal padding.
- **Divider:** `.e-theme-duo__rule` — vertical 1px between circles.
- **Circles:** ~16px, round fills only; **no** `is-active` outer ring on accent.
- Hover: subtle scale/opacity — not a selection outline.

## Legacy (avoid unless user requests)

- `border-radius: 999px` on `.e-theme-duo`
- `e-mode-toggles` ☾/☀ pill
- `e-accent-row` of all tints
- `ThemeCompact` popover

## Verify

```bash
cd dashboard && npm run build:embedded
```

Hard-refresh relay `/ui/`.