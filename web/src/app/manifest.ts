import type { MetadataRoute } from "next";
import { PRODUCT_DESCRIPTION, PRODUCT_NAME } from "@/lib/product";

// The export writes this once, as /manifest.webmanifest.
export const dynamic = "force-static";

// Icons: web/scripts/brand-icons.mjs renders them (DESIGN.md "Brand").
// The splash and app tile are deep water (tokens.css dark --background).
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: PRODUCT_NAME,
    short_name: PRODUCT_NAME,
    description: PRODUCT_DESCRIPTION,
    id: "/",
    start_url: "/",
    scope: "/",
    display: "standalone",
    background_color: "#0c1f25",
    theme_color: "#08171c",
    icons: [
      {
        src: "/icon-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icon-maskable-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "maskable",
      },
      {
        src: "/icon-maskable-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
