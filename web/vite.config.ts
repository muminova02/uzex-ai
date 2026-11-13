import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: Vite on :5173 proxies /api to the FastAPI server on :8000 (UZEX_WEB_PORT).
// Prod: `npm run build` writes web/dist, which FastAPI serves on the same origin as /api.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true, proxy: { "/api": "http://127.0.0.1:8000" } },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
});
