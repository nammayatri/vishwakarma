import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the orchestrator at /console — base must match.
export default defineConfig({
  plugins: [react()],
  base: "/console/",
  server: {
    proxy: { "/api": "http://localhost:5050" },
  },
});
