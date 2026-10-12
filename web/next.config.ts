import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static export: the FastAPI gateway serves web/out at the origin root
  // (gateway/ui_routes.py, pages from gateway/ui_pages.json). No SSR/Node in
  // production (see docs/design/WEB_UI_STEERING.md).
  output: "export",
  // The gateway serves each page at /<name>/ from <name>/index.html.
  // trailingSlash makes the export emit that shape; the gateway answers the
  // slashless form with a 308 to the slash form.
  trailingSlash: true,
  images: {
    unoptimized: true,
  },

  // DEV ONLY: `next dev` sends every path it does not serve itself to the
  // gateway, so the app can call same-origin paths like fetch("/admin/api/...").
  // Rewrites are NOT applied to the static export — `next build` ignores them
  // with output: "export". In production the gateway serves the app and its
  // APIs on the same origin, so no proxy is needed.
  async rewrites() {
    return {
      fallback: [
        {
          source: "/:path*",
          destination: "http://127.0.0.1:5000/:path*",
        },
      ],
    };
  },
};

export default nextConfig;
