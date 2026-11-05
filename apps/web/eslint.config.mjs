import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const config = [
  ...nextVitals,
  ...nextTs,
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  // Playwright script run with plain Node (CommonJS), outside the app bundle
  { files: ["e2e/**/*.cjs"], rules: { "@typescript-eslint/no-require-imports": "off" } },
];

export default config;
