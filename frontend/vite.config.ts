import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../web",
    // The backend stores generated article images under web/generated-images.
    // Do not erase them whenever the React bundle is rebuilt.
    emptyOutDir: false,
  },
});
