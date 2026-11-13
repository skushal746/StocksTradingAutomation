import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the Vite server forwards API paths to the FastAPI server on :8000,
// so the browser sees one origin and no CORS setup is needed. Production uses nginx (see nginx.conf).
const API_PATHS = ["/auth", "/news", "/watchlist", "/trades", "/health"];
const apiTarget = process.env.API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(API_PATHS.map((path) => [path, { target: apiTarget, changeOrigin: true }])),
  },
});
