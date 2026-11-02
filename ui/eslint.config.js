import js from "@eslint/js";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage", "node_modules", "src/api/types.gen.ts"] },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  jsxA11y.flatConfigs.strict,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      globals: globals.browser,
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // An unnamed control is the failure that matters most on a security console.
      "jsx-a11y/control-has-associated-label": "error",
      // `any` and non-null assertions are how a typed boundary quietly stops being one.
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-non-null-assertion": "error",
      "@typescript-eslint/restrict-template-expressions": ["error", { allowNumber: true }],
    },
  },
  {
    ...tseslint.configs.disableTypeChecked,
    files: ["scripts/**/*.mjs", "*.config.js"],
    languageOptions: { globals: globals.node },
  },
);
