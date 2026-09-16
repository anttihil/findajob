// @ts-check
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

// The `lint` script points eslint at `findajob/web/frontend-src` directly (not `.`),
// so this config only ever sees files under that tree -- no repo-wide ignore list needed.
const FRONTEND_FILES = ["**/*.{ts,tsx}"];

export default tseslint.config(
  { files: FRONTEND_FILES, ...js.configs.recommended },
  ...tseslint.configs.recommended.map((config) => ({ ...config, files: FRONTEND_FILES })),
  {
    files: FRONTEND_FILES,
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
    },
  }
);
