import { defineConfig } from "vitest/config";
import preact from "@preact/preset-vite";

export default defineConfig({
  root: "careerradar/web/frontend-src",
  plugins: [preact()],
  test: {
    environment: "jsdom",
    globals: true,
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
