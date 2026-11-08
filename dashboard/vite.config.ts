/// <reference types="vitest/config" />
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import type { Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url));

/**
 * Third-party notices that travel with the build, in dist/licenses/<package>/. The LICENSE comes from the installed
 * package; the NOTICE too when the package ships it, otherwise from the copy kept next to the notices screen (the
 * published package of the chart library has no NOTICE file).
 */
const NOTICES = [
  {
    name: "lightweight-charts",
    license: here("./node_modules/lightweight-charts/LICENSE"),
    notice: [here("./node_modules/lightweight-charts/NOTICE"), here("./src/screens/system/licenses/lightweight-charts/NOTICE")],
  },
];

function thirdPartyNotices(): Plugin {
  return {
    name: "hokma-third-party-notices",
    apply: "build",
    generateBundle() {
      for (const pkg of NOTICES) {
        const notice = pkg.notice.find((file) => existsSync(file));
        if (!existsSync(pkg.license) || !notice) this.error(`Missing LICENSE or NOTICE of ${pkg.name}`);
        this.emitFile({ type: "asset", fileName: `licenses/${pkg.name}/LICENSE`, source: readFileSync(pkg.license) });
        this.emitFile({ type: "asset", fileName: `licenses/${pkg.name}/NOTICE`, source: readFileSync(notice!) });
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), thirdPartyNotices()],
  resolve: { alias: { "@": here("./src") } },
  server: {
    proxy: {
      "/health": "http://127.0.0.1:8765",
      "/api": { target: "http://127.0.0.1:8765", ws: true },
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
      "/rpc": { target: "ws://127.0.0.1:8765", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
