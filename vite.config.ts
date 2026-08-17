import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// Points at the FastAPI dev server (`careerradar serve` / `uvicorn careerradar.web.app:app`),
// which defaults to 127.0.0.1:8000. Override it if that port is taken.
const apiProxyTarget = process.env.CAREERRADAR_API_PROXY || "http://127.0.0.1:8000";

export default defineConfig(({ command }) => ({
  root: "careerradar/web/frontend-src",
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
    proxy: {
      "/api": apiProxyTarget,
    },
  },
}));
