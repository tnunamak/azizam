# web/ — gateway UI (Next.js static export)

The React rebuild of the gateway UI. Binding spec:
[`docs/design/WEB_UI_STEERING.md`](../docs/design/WEB_UI_STEERING.md) — read it
before touching anything here.

## Stack

- Next.js 15 App Router, React 19, TypeScript strict, Tailwind v4
- `output: 'export'` — static files only; the FastAPI gateway serves
  `web/out/` under `/ui`. No SSR/Node in production.
- All visual tokens come from `@vivid-fish/brand`. This deployment uses a local
  path dependency, but the package name and token contract are portable; token
  gaps are fixed upstream in the brand package, never with local literals.

## Commands

```bash
npm run dev      # dev server; proxies /admin/api/* and /me/api/* to the
                 # gateway on 127.0.0.1:5000 via rewrites (dev only — the
                 # static export ignores rewrites; production is same-origin)
npm run check    # tsc --noEmit + next lint + react-doctor (errors fail) + next build
npm run build    # static export to out/ with /ui basePath
npm run format   # prettier (with Tailwind class sorting)
```

Note: `npm run dev` uses webpack, not Turbopack — Turbopack panics on the
symlinked brand package outside the project root ("leaves the filesystem
root"). Production builds are unaffected.

## Fonts

Brand webfonts are **self-hosted** via npm packages — no Google Fonts CDN.

| Family | Package | Brand token |
|--------|---------|-------------|
| Geist | `geist` | `--vf-font-sans` |
| JetBrains Mono | `@fontsource-variable/jetbrains-mono` | `--vf-font-mono` |
| Newsreader | `@fontsource-variable/newsreader` | `--vf-font-serif` |

`@font-face` rules live in `src/app/fonts.css` (imported by `globals.css`).
The family names are registered exactly as the brand tokens require (the
fontsource packages register "…Variable" names so we write custom rules).
`next build` copies the woff2 files into `out/_next/static/media/` — all
same-origin; the gateway CSP (`default-src 'self'`) covers them.

## Pre-commit hook (one-time setup)

```bash
ln -sf ../../scripts/pre-commit-web.sh .git/hooks/pre-commit
```

Runs `npm run check` whenever staged changes touch `web/**`.
