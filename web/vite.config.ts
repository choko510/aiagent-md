import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command, mode }) => {
  // Only the Vite dev proxy sees this token. It is never injected into client
  // code or VITE_* variables. Build is deliberately independent of dev auth.
  const token = loadEnv(mode, ".", "").AIAGENT_MARKETPLACE_DEV_TOKEN;
  if (command === "serve" && (!token || token.length < 32)) {
    throw new Error("Set AIAGENT_MARKETPLACE_DEV_TOKEN (32+ chars) for the local dev proxy");
  }
  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8001",
          headers: token ? { Authorization: "Bearer " + token } : {},
        },
      },
    },
  };
});
