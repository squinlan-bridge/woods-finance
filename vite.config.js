import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite reads .jsx files anywhere in the project — woods_finance_app.jsx lives at repo root.
export default defineConfig({
  plugins: [react()],
  server: { port: 5174, open: true },
});
