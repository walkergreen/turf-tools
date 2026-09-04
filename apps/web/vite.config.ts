import tailwindcss from "@tailwindcss/vite";
import { tanstackStart } from "@tanstack/react-start/plugin/vite";
import react from "@vitejs/plugin-react";
import { nitro } from "nitro/vite";
import { defineConfig, loadEnv } from "vite-plus";

const isTest = process.env.NODE_ENV === "test" || !!process.env.VITEST;

export default defineConfig(({ mode }) => ({
  resolve: {
    tsconfigPaths: true,
  },
  server: {
    port: 3000,
    host: "0.0.0.0",
    // Hostnames a reverse proxy or tunnel may present, comma-separated in
    // WEB_ALLOWED_HOSTS; the dev server rejects any other non-local Host.
    // Read through loadEnv because the config is evaluated before the app's
    // env file reaches process.env.
    allowedHosts: (loadEnv(mode, process.cwd(), "").WEB_ALLOWED_HOSTS ?? "")
      .split(",")
      .map((host) => host.trim())
      .filter((host) => host.length > 0),
  },
  ssr: {
    noExternal: ["@turf-tools/db", "@electric-sql/pglite"],
  },
  plugins: isTest
    ? []
    : [
        // Server-rendered: route loaders run during SSR and their query data
        // ships with the document, so a page paints with content instead of
        // fetching it after hydration. The trade is time-to-first-byte —
        // the server now runs auth + loaders before it can respond, and the
        // browser holds the previous paint until it does.
        tanstackStart(),
        nitro(),
        // React's plugin must come after Start's.
        react(),
        tailwindcss(),
      ],
}));
