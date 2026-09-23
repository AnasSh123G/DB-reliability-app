// @lovable.dev/vite-tanstack-config already includes the following — do NOT add them manually
// or the app will break with duplicate plugins:
//   - TanStack devtools (dev-only, first), tanstackStart, viteReact, tailwindcss, tsConfigPaths,
//     nitro (build-only using cloudflare as a default target), VITE_* env injection, @ path alias,
//     React/TanStack dedupe, error logger plugins, and sandbox detection (port/host/strictPort).
// You can pass additional config via defineConfig({ vite: { ... }, etc... }) if needed.
import { defineConfig } from "@lovable.dev/vite-tanstack-config";

export default defineConfig({
  // Self-hosted Node server instead of the Cloudflare Worker default -- run
  // as its own process behind dbrel's reverse proxy (see design.md decision 6,
  // replace-web-ui-with-bahn-buddy: the SPA static-shell path was tried first
  // and found broken in this toolchain version).
  nitro: { preset: "node" },
});
