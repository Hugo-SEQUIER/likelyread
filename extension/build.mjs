// Build the extension into dist/ (load it unpacked from there).
import * as esbuild from "esbuild";
import { cpSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";

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

const CLOUD_URL = (process.env.CLOUD_URL || "https://app.likelyread.com").replace(/\/+$/, "");
const define = { __CLOUD_URL__: JSON.stringify(CLOUD_URL) };

const OUT = process.env.OUT_DIR || "dist";
rmSync(OUT, { recursive: true, force: true });
mkdirSync(OUT, { recursive: true });

const options = {
  entryPoints: {
    background: "src/background.ts",
    content: "src/content.ts",
    popup: "src/popup/popup.ts",
    options: "src/options/options.ts",
  },
  outdir: OUT,
  bundle: true,
  format: "iife",
  target: "chrome111",
  logLevel: "info",
  define,
};

// The cloud origin appears in the manifest twice: host access for the API, and
// externally_connectable so the sign-in page can hand the token to the extension.
const manifest = JSON.parse(readFileSync("manifest.json", "utf8"));
const cloudMatch = `${new URL(CLOUD_URL).protocol}//${new URL(CLOUD_URL).hostname}/*`;
manifest.host_permissions = [...new Set([...manifest.host_permissions.filter((m) => m !== "https://app.likelyread.com/*"), cloudMatch])];
manifest.externally_connectable = { matches: [cloudMatch] };
writeFileSync(`${OUT}/manifest.json`, JSON.stringify(manifest, null, 2) + "\n");
cpSync("icons", `${OUT}/icons`, { recursive: true });
for (const f of ["popup/popup.html", "popup/popup.css", "options/options.html", "options/options.css", "content.css", "ui/fonts.css"]) {
  cpSync(`src/${f}`, `${OUT}/${f.split("/").pop()}`);
}
cpSync("src/ui/fonts", `${OUT}/fonts`, { recursive: true });

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
} else {
  await esbuild.build(options);
}
