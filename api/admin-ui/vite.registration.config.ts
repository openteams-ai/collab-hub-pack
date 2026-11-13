import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The registration pages: the invitation-acceptance page an invitee opens
// before they have an account. Built as its own bundle, into its own directory,
// so that what is served publicly contains none of the admin panel's code. The
// API serves this directory from `/invite`; see `routers/invite.py`.
//
// `base` is relative for the reason it is in the panel's config: the API can be
// served under a rootPath prefix, and absolute asset URLs would 404 there.
//
// Font files keep their own names (no content hash). The server-rendered pages
// under `/web` are set in the same typeface and their stylesheet, written in
// Python, refers to these files by name; a hash it cannot know would break
// that. Nothing is lost: every response on the surface is `no-store`, so the
// hash bought no cache busting here.
export default defineConfig({
  root: "registration",
  base: "./",
  plugins: [react()],
  build: {
    outDir: "../dist/registration",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        assetFileNames: (asset) => {
          const name = asset.names?.[0] ?? "";
          return /\.woff2?$/.test(name) ? "assets/[name][extname]" : "assets/[name]-[hash][extname]";
        },
      },
    },
  },
});
