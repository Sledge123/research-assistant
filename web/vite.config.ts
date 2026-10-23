import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  base: "/highscore/",
  plugins: [vue()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/highscore/api": {
        target: "http://127.0.0.1:8787",
        changeOrigin: false,
      },
    },
  },
});
