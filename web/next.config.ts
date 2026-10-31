import type { NextConfig } from "next";

const API_INTERNAL = process.env.API_INTERNAL_BASE ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // The pay page is opened from emails, also on phones through a tunnel (cloudflared): the dev server
  // must accept that host, and the page reaches the API through this server, not directly.
  allowedDevOrigins: ["*.trycloudflare.com"],
  async rewrites() {
    return [
      {
        source: "/pay-api/:orderId",
        destination: `${API_INTERNAL}/mock-pay/:orderId`,
      },
    ];
  },
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
