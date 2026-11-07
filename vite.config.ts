import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// Start the backend separately with `uv run findajob start --dev`.
const apiProxyTarget = process.env.FIND_A_JOB_API_PROXY || "http://127.0.0.1:8010";

export default defineConfig(({ command }) => ({
  root: "findajob/web/frontend-src",
  // Only the production build needs to live under /static/dist/ -- that's where FastAPI's
  // StaticFiles mount mounts it. The dev server serves its own module graph at "/" directly;
  // setting the same base there would collide with the /api proxy target's own routes.
  base: command === "build" ? "/static/dist/" : "/",
  plugins: [preact()],
  build: {
    outDir: "../frontend/dist",
    emptyOutDir: true,
  },
  server: {
    host: "127.0.0.1",
    strictPort: true,
    proxy: {
      "/api": apiProxyTarget,
    },
  },
}));
