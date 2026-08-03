/**
 * CoreMesh frontend development, build, and unit-test configuration.
 *
 * System role: produces the static React bundle served on port 3000.
 * Dependencies: Vite, its React plugin, and Vitest's JSDOM environment.
 * Side effects: development opens a local listener; builds write dist/.
 */
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 3000,
  },
  preview: {
    host: "0.0.0.0",
    port: 3000,
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: true,
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
});
