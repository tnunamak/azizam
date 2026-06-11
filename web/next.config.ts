import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static export: the FastAPI gateway serves web/out under /ui via
  // StaticFiles. No SSR/Node in production (see docs/design/WEB_UI_STEERING.md).
  output: "export",
  basePath: "/ui",
  assetPrefix: "/ui",
  // Starlette StaticFiles(html=True) resolves directories to index.html but
  // never maps /admin/apps -> admin/apps.html. trailingSlash makes the
  // export emit admin/apps/index.html, so deep links like /ui/admin/apps
  // work (Starlette redirects to the slash form, then serves the index).
  trailingSlash: true,
  images: {
    unoptimized: true,
  },

  // DEV ONLY: `next dev` proxies the gateway JSON APIs so the app can call
  // same-origin paths like fetch("/admin/api/..."). Rewrites are NOT applied
  // to the static export — `next build` ignores them with output: "export".
  // In production the gateway mounts the built assets at /ui on the same
  // origin as /admin/api/* and /me/api/*, so no proxy is needed.
  // `basePath: false` keeps the API paths un-prefixed (the APIs live at the
  // origin root, not under /ui).
  async rewrites() {
    return [
      {
        source: "/admin/api/:path*",
        destination: "http://127.0.0.1:5000/admin/api/:path*",
        basePath: false,
      },
      {
        source: "/me/api/:path*",
        destination: "http://127.0.0.1:5000/me/api/:path*",
        basePath: false,
      },
    ];
  },
};

export default nextConfig;
