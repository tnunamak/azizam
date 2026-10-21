import type { Metadata } from "next";
import {
  PRODUCT_DESCRIPTION,
  PRODUCT_NAME,
  PRODUCT_TAGLINE,
} from "@/lib/product";

// The icons and the manifest come from Next's file conventions (app/favicon.ico,
// app/icon.svg, app/apple-icon.png, app/manifest.ts). A link preview needs an
// absolute image URL, and a static export knows its origin only at build time:
// build with AZIZAM_PUBLIC_URL set (scripts/safe-restart passes its environment
// to the build), or the preview keeps its title and description and has no image.
// A file-convention opengraph-image would point at http://localhost:3000 instead.
export function publicOrigin(value: string | undefined): URL | undefined {
  const text = value?.trim();
  if (!text) return undefined;
  try {
    const url = new URL(text);
    return url.protocol === "https:" || url.protocol === "http:"
      ? url
      : undefined;
  } catch {
    return undefined;
  }
}

export function siteMetadata(origin: URL | undefined): Metadata {
  return {
    title: { default: PRODUCT_NAME, template: `%s · ${PRODUCT_NAME}` },
    description: PRODUCT_DESCRIPTION,
    applicationName: PRODUCT_NAME,
    appleWebApp: { title: PRODUCT_NAME },
    ...(origin && { metadataBase: origin }),
    openGraph: {
      type: "website",
      siteName: PRODUCT_NAME,
      title: PRODUCT_NAME,
      description: PRODUCT_DESCRIPTION,
      ...(origin && {
        images: [
          {
            url: "/opengraph-image.png",
            width: 1200,
            height: 630,
            alt: `${PRODUCT_NAME}. ${PRODUCT_TAGLINE}`,
          },
        ],
      }),
    },
    twitter: { card: origin ? "summary_large_image" : "summary" },
    // Private service: the page itself says noindex, as the gateway's header and robots.txt do.
    robots: { index: false, follow: false },
  };
}
