import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// API_PROXY_TARGET points at the FastAPI dev server (`careerradar serve`), which
// defaults to 127.0.0.1:8000. Override it if that port is taken.
const apiProxyTarget = process.env.CAREERRADAR_API_PROXY || "http://127.0.0.1:8000";

export default defineConfig({
  root: "careerradar/web/frontend-src",
  base: "/static/dist/",
  plugins: [preact()],
  build: {
    outDir: "../frontend/dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": apiProxyTarget,
      "/static": apiProxyTarget,
    },
  },
});
