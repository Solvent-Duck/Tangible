import type { NextConfig } from "next";

// The FastAPI workbench. /api/* is proxied to it, so the browser talks to one origin (no CORS)
// and the session token rides along as a Bearer header set by the client. Rewrites are fixed at
// build time, so a container build needs TANGIBLE_API_URL as a build argument.
const api = (process.env.TANGIBLE_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  // A self-contained server for the container image (web/Dockerfile).
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    const base = [
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "X-Frame-Options", value: "DENY" },
      { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
      { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
      { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
    ];
    return [
      { source: "/:path*", headers: base },
      // The invite token is in the URL: never send it on as a Referer.
      { source: "/invite/:path*", headers: [{ key: "Referrer-Policy", value: "no-referrer" }] },
    ];
  },
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
