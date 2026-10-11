import type { NextConfig } from "next";

// The FastAPI workbench. /api/* is proxied to it, so the browser talks to one origin (no CORS)
// and the session token rides along as a Bearer header set by the client.
const api = (process.env.TANGIBLE_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${api}/api/:path*` }];
  },
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
