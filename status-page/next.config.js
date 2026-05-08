/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  images: { unoptimized: true },
  trailingSlash: false,
  // Static export served by FastAPI StaticFiles at the proxy root.
  // No basePath: page lives at "/".
};

module.exports = nextConfig;
