import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, ".", "VITE_");

  return {
    plugins: [react()],
    server: {
      allowedHosts: ["localhost", "127.0.0.1", "frontend"],
      host: "0.0.0.0",
      headers: {
        "Cache-Control": "no-store, private",
        "Referrer-Policy": "no-referrer",
      },
      port: 5173,
      proxy: {
        "/api": {
          target: environment.VITE_API_PROXY_TARGET || "http://api:8000",
          changeOrigin: true,
        },
        "/health": {
          target: environment.VITE_API_PROXY_TARGET || "http://api:8000",
          changeOrigin: true,
        },
      },
    },
  };
});
