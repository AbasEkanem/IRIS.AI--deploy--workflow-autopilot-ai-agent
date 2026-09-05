import type { NextConfig } from "next";

// Where the FastAPI backend lives, as seen FROM THE NEXT SERVER (not the browser).
// Rewrites are server-side proxies, so 127.0.0.1 only works when Next and FastAPI
// share a network namespace — true for local dev, false for separate containers.
// In docker-compose the backend is reachable by service name, hence the override.
//
// .trim() is NOT cosmetic. Railway's dashboard preserves a leading space in a
// pasted value, and the live UI service has exactly that:
// BACKEND_ORIGIN=" https://irisai-…up.railway.app". An untrimmed value makes every
// rewrite destination below the invalid URL " https://…/ask", so the whole proxy
// layer (/ask, /resume, /api/threads, /google/*) breaks — while the app still
// builds and serves, which is what makes it hard to spot. Same for the public URL:
// browsers happen to tolerate leading whitespace in fetch(), so that one fails
// less visibly, but neither should depend on that.
const BACKEND_ORIGIN = (process.env.BACKEND_ORIGIN || "http://127.0.0.1:8000").trim().replace(/\/$/, "");

const nextConfig: NextConfig = {
  // Required for the Docker image: emits .next/standalone with a self-contained
  // server.js and a pruned node_modules. ui/Dockerfile COPYs that directory, so
  // without this flag the build produces nothing for it to copy.
  output: "standalone",
  env: {
    // .trim() for the same reason as BACKEND_ORIGIN above — the live Railway value
    // carries a leading space. This one is baked into the client bundle at build
    // time and becomes the base of every fetch() in ui/src/lib/api.ts, so the
    // whitespace ships to the browser.
    NEXT_PUBLIC_API_URL: (process.env.NEXT_PUBLIC_API_URL || "").trim(),
  },
  images: {
    remotePatterns: [
      { protocol: "https", hostname: "images.unsplash.com" },
      { protocol: "http",  hostname: "localhost" },
      { protocol: "http",  hostname: "127.0.0.1" },
    ],
  },
  async rewrites() {
    return [
      { source: '/ask',                    destination: `${BACKEND_ORIGIN}/ask` },
      { source: '/health',                 destination: `${BACKEND_ORIGIN}/health` },
      { source: '/api/threads/:path*',     destination: `${BACKEND_ORIGIN}/api/threads/:path*` },
      { source: '/api/upload',             destination: `${BACKEND_ORIGIN}/api/upload` },
      // The backend route is `/api/greeting` (web_api.py:1570), NOT `/greeting` —
      // rewriting the prefix away made every subline request a 404, which the
      // client swallowed silently (page.tsx:977) and fell back to the static
      // greeting. The path is the same on both sides; keep it that way.
      { source: '/api/greeting',           destination: `${BACKEND_ORIGIN}/api/greeting` },
      { source: '/auth/me',                destination: `${BACKEND_ORIGIN}/auth/me` },
      { source: '/auth/logout',            destination: `${BACKEND_ORIGIN}/auth/logout` },
      // Proxy static files (FLUX-generated greeting images) from the FastAPI backend
      { source: '/static/:path*',          destination: `${BACKEND_ORIGIN}/static/:path*` },
      // Endpoints api.ts calls that previously had no rewrite: they only worked
      // when NEXT_PUBLIC_API_URL pointed straight at the backend. Proxying them
      // keeps same-origin (relative-URL) mode working, cookies included — which
      // /google/callback's HttpOnly state+verifier cookies depend on.
      { source: '/resume',                 destination: `${BACKEND_ORIGIN}/resume` },
      { source: '/google/:path*',           destination: `${BACKEND_ORIGIN}/google/:path*` },
    ];
  },
};

export default nextConfig;

