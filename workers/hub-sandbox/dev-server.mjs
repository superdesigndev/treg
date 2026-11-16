import { readFileSync, writeSync } from "node:fs";
import { Miniflare } from "miniflare";

const secret = process.env.AUTH_SECRET;
if (!secret) {
  writeSync(2, "AUTH_SECRET is required\n");
  process.exit(1);
}

const script = readFileSync(new URL("./src/index.js", import.meta.url), "utf8");
const mf = new Miniflare({
  modules: true,
  script,
  compatibilityDate: "2026-08-06",
  bindings: { AUTH_SECRET: secret },
  workerLoaders: { LOADER: {} },
  host: "127.0.0.1",
  port: 0,
});
const url = await mf.ready;
writeSync(1, `READY ${url.origin}\n`);

process.on("SIGTERM", () => {
  mf.dispose().then(() => process.exit(0), () => process.exit(0));
});
await new Promise(() => {});
