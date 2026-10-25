/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const API_TARGET = process.env.SWARMOS_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "prompt",
      injectRegister: false,
      includeAssets: ["icons/favicon.svg", "icons/apple-touch-icon.png"],
      manifest: {
        name: "SwarmOS — City Operations",
        short_name: "SwarmOS",
        description: "Intelligent operations center for Indian cities: live data, incidents and 50 local AI agents.",
        start_url: "/",
        scope: "/",
        display: "standalone",
        orientation: "any",
        theme_color: "#0c0e1f",
        background_color: "#0c0e1f",
        categories: ["productivity", "utilities"],
        icons: [
          { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
          { src: "/icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
      workbox: {
        // App shell + static assets are precached. Privileged API responses are NEVER cached.
        globPatterns: ["**/*.{js,mjs,css,html,svg,png,woff2}"],
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api\//],
        cleanupOutdatedCaches: true,
        // A new deployment takes over as soon as it is downloaded (no "close every tab" step); the open page is
        // not reloaded — the next load, or the "New version" button, shows the update.
        skipWaiting: true,
        clientsClaim: true,
        maximumFileSizeToCacheInBytes: 3 * 1024 * 1024,
        runtimeCaching: [
          { urlPattern: ({ url }) => url.pathname.startsWith("/api/"), handler: "NetworkOnly" },
        ],
      },
    }),
  ],
  server: { port: 5173, proxy: { "/api": { target: API_TARGET, changeOrigin: false } } },
  preview: { port: 4173, proxy: { "/api": { target: API_TARGET, changeOrigin: false } } },
  build: {
    target: "es2022",
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  test: { environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"] },
});
