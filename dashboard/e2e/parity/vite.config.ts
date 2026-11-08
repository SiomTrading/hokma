import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/** Builds the parity page (T-1) on its own: `npx vite build --config e2e/parity/vite.config.ts`. */
export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  base: "./",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("../../src", import.meta.url)) } },
  publicDir: fileURLToPath(new URL("../../public", import.meta.url)),
  build: { outDir: fileURLToPath(new URL("../artifacts/parity-dist", import.meta.url)), emptyOutDir: true },
});
