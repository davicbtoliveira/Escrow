import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, ".", "VITE_");

  return {
    plugins: [react()],
    server: {
      host: "0.0.0.0",
      port: 5173,
      proxy: {
        "/health": {
          target: environment.VITE_API_PROXY_TARGET || "http://api:8000",
          changeOrigin: true,
        },
      },
    },
  };
});
