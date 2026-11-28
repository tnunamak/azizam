import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/**/*.spec.{ts,tsx}", "src/**/*.test.{ts,tsx}"],
    rules: {
      "no-restricted-globals": [
        "error",
        {
          name: "fetch",
          message:
            "Use networkFetch from @/lib/network-error so the session guard applies. An allowlisted endpoint needs an eslint-disable comment that gives the reason.",
        },
      ],
    },
  },
  {
    // endingRequest is the sign-out's own request helper: it bypasses the session guard on purpose, so only the sign-out
    // module may import it.
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/lib/session-end.ts"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          // A pattern, not a path: it catches "@/lib/network-error" and relative imports of the same module alike.
          patterns: [
            {
              group: ["**/network-error"],
              importNames: ["endingRequest"],
              message: "endingRequest is the sign-out's own request helper and bypasses the session guard. Only src/lib/session-end.ts may import it.",
            },
          ],
        },
      ],
    },
  },
  {
    ignores: [
      "node_modules/**",
      ".next/**",
      "out/**",
      "build/**",
      "next-env.d.ts",
    ],
  },
];

export default eslintConfig;
