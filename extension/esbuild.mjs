import { build, context } from "esbuild";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const watch = process.argv.includes("--watch");

const host = {
  entryPoints: [join(here, "src/extension.ts")],
  bundle: true, platform: "node", format: "cjs", target: "node18",
  external: ["vscode"], outfile: join(here, "dist/extension.js"), sourcemap: true, logLevel: "info",
};
const ui = {
  entryPoints: [join(here, "../ui/src/main.tsx")],
  bundle: true, platform: "browser", format: "iife", target: "es2020", jsx: "automatic",
  outdir: join(here, "dist/webview"), entryNames: "main", minify: true, logLevel: "info",
  nodePaths: [join(here, "../ui/node_modules")],
  define: { "process.env.NODE_ENV": '"production"' },
};

if (watch) {
  for (const cfg of [host, ui]) await (await context(cfg)).watch();
} else {
  await build(host);
  await build(ui);
}
