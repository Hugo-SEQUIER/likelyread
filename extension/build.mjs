// Build the extension into dist/ (load it unpacked from there).
import * as esbuild from "esbuild";
import { cpSync, mkdirSync, readdirSync, rmSync } from "node:fs";

const watch = process.argv.includes("--watch");
const tests = process.argv.includes("--tests");

if (tests) {
  rmSync(".test-dist", { recursive: true, force: true });
  await esbuild.build({
    entryPoints: readdirSync("test").filter((f) => f.endsWith(".test.ts")).map((f) => `test/${f}`),
    outdir: ".test-dist",
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node20",
    outExtension: { ".js": ".mjs" },
    external: ["jsdom"],
    logLevel: "warning",
  });
  process.exit(0);
}

rmSync("dist", { recursive: true, force: true });
mkdirSync("dist", { recursive: true });

const options = {
  entryPoints: {
    background: "src/background.ts",
    content: "src/content.ts",
    popup: "src/popup/popup.ts",
    options: "src/options/options.ts",
  },
  outdir: "dist",
  bundle: true,
  format: "iife",
  target: "chrome111",
  logLevel: "info",
};

cpSync("manifest.json", "dist/manifest.json");
cpSync("icons", "dist/icons", { recursive: true });
for (const f of ["popup/popup.html", "popup/popup.css", "options/options.html", "options/options.css", "content.css", "ui/fonts.css"]) {
  cpSync(`src/${f}`, `dist/${f.split("/").pop()}`);
}
cpSync("src/ui/fonts", "dist/fonts", { recursive: true });

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
} else {
  await esbuild.build(options);
}
