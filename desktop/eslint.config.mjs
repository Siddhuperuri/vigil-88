import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["out", "coverage", "node_modules", "test-results", "playwright-report"] },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  {
    files: ["**/*.ts", "**/*.mts"],
    languageOptions: {
      globals: globals.node,
      parserOptions: { project: "./tsconfig.lint.json", tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-non-null-assertion": "error",
      "@typescript-eslint/restrict-template-expressions": ["error", { allowNumber: true }],
    },
  },
  {
    ...tseslint.configs.disableTypeChecked,
    files: ["**/*.mjs", "**/*.js"],
    languageOptions: { globals: globals.node },
  },
);
