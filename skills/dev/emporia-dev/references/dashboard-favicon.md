# Dashboard tab icon (favicon)

## Goal

Browser tab shows **Nous Research / Hermes** branding (same family as Portal / Hermes Agent web), not a generic Vite default.

## Source assets

When `portal.nousresearch.com` is unreachable (Vercel 429/challenge from automation), pull public icons from:

- `https://hermes-agent.nousresearch.com/favicon.ico`
- `https://hermes-agent.nousresearch.com/icon.png`

Store under **`dashboard/public/`** (Vite copies verbatim to `dist/` on build).

## `index.html`

```html
<link rel="icon" href="favicon.ico" sizes="48x48" type="image/x-icon" />
<link rel="icon" href="icon.png" sizes="48x48" type="image/png" />
<link rel="apple-touch-icon" href="icon.png" />
```

Use **relative** `href` so embedded `base: /ui/` resolves to `/ui/favicon.ico` at runtime.

## Ship

```bash
cd dashboard && npm run build:embedded
```

Hard-refresh `/ui/` (browsers cache favicons aggressively).

## Pitfalls

| Issue | Fix |
|-------|-----|
| Tab still blank after build | Confirm `dist/favicon.ico` exists; open `/ui/favicon.ico` directly |
| Wrong icon on dev `:5173` | Same `public/` files; restart `npm run dev` if added mid-session |
| Operator wants exact Portal file only | Replace `public/favicon.ico` / `icon.png` manually; keep same `index.html` links |

Legacy `public/favicon.svg` (purple Nous mark) can stay for other uses; tab links should prefer **ico/png** above.