// Loader for one hub script. The dynamic worker has no outbound network. ctx.call is a
// loopback binding; the fetch back to treg runs in this isolate, which holds the per-run
// bridge token and no provider key.
import { WorkerEntrypoint } from "cloudflare:workers";

const COMPAT = "2026-08-06";

const GUEST = `
import { WorkerEntrypoint } from "cloudflare:workers";

const BLOCKED = ["fetch", "connect", "require", "process", "setTimeout", "setInterval", "XMLHttpRequest", "WebSocket", "os"];

function hideRoots() {
  for (const name of BLOCKED) {
    try {
      Object.defineProperty(globalThis, name, { value: undefined, writable: true, configurable: true });
    } catch (e) {
      try { globalThis[name] = undefined; } catch (e2) { /* a non-configurable intrinsic stays, and globalOutbound still throws */ }
    }
  }
}

// workerd's own wording when globalOutbound is null. Anything else means the isolate can dial out.
async function networkClosed() {
  try {
    await fetch("http://127.0.0.1:1/");
    return false;
  } catch (e) {
    return String(e && e.message || e).indexOf("not permitted") !== -1;
  }
}

function csv(text) {
  const rows = []; let row = [], field = "", q = false, i = 0; const s = String(text);
  while (i < s.length) {
    const c = s[i];
    if (q) {
      if (c === '"') { if (s[i + 1] === '"') { field += '"'; i += 2; continue; } q = false; i++; continue; }
      field += c; i++; continue;
    }
    if (c === '"') { q = true; i++; continue; }
    if (c === ",") { row.push(field); field = ""; i++; continue; }
    if (c === "\\r") { i++; continue; }
    if (c === "\\n") { row.push(field); rows.push(row); row = []; field = ""; i++; continue; }
    field += c; i++;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  if (!rows.length) return [];
  const head = rows[0].map(h => h.trim());
  return rows.slice(1).filter(r => r.length > 1 || (r.length === 1 && r[0] !== "")).map(r => {
    const o = {}; head.forEach((h, k) => { o[h] = r[k] === undefined ? "" : r[k]; }); return o;
  });
}

function makeCtx(bridge, inputs, data) {
  const logs = [];
  // QuickJS delivers charge and call before charge()/call() return to the script. A worker
  // fetch cannot do that, so a script that does not await still cannot finish ahead of them.
  const pending = [];
  function track(p) {
    pending.push(Promise.resolve(p));
    return p;
  }
  const ctx = {
    inputs: inputs,
    data: data,
    logs: logs,
    call(target, opts) {
      return track(bridge.op({ op: "call", target: String(target), opts: opts || {} }).then((reply) => {
        if (!reply || reply.op === "refused") throw new Error((reply && reply.error) || "refused");
        return {
          status: reply.status, headers: reply.headers, json: reply.json, text: reply.text,
          truncated: !!reply.truncated, timed_out: !!reply.timed_out, cost_usd: reply.cost_usd || 0,
        };
      }));
    },
    csv: csv,
    log(text) { logs.push(String(text)); },
    charge(usd, label) {
      return track(bridge.op({ op: "charge", usd: Number(usd), label: String(label === undefined ? "" : label) }).then((reply) => {
        if (!reply || reply.op === "refused") throw new Error((reply && reply.error) || "refused");
      }));
    },
  };
  return { ctx: ctx, settle: () => Promise.all(pending) };
}

function failureKind(text) {
  const folded = text.toLowerCase();
  if (folded.indexOf("out of memory") !== -1 || folded.indexOf("memory limit") !== -1) return "memory";
  if (folded.indexOf("stack") !== -1) return "stack";
  return "script";
}

export default class Guest extends WorkerEntrypoint {
  async run(payload) {
    if (!(await networkClosed())) {
      return { op: "error", kind: "protocol", message: "the sandbox has an open network" };
    }
    hideRoots();
    let mod;
    try {
      mod = await import("./user.js");
    } catch (e) {
      return { op: "error", kind: "script", message: String(e && e.message || e).slice(0, 600) };
    }
    const run = mod.default;
    if (typeof run !== "function") {
      return { op: "error", kind: "script", message: "run.js must export default async function run(ctx)" };
    }
    const made = makeCtx(this.env.BRIDGE, payload.inputs || {}, payload.data == null ? null : payload.data);
    const ctx = made.ctx;
    const flush = async () => {
      if (!ctx.logs.length) return;
      const lines = ctx.logs.splice(0, ctx.logs.length);
      await this.env.BRIDGE.op({ op: "logs", lines: lines });
    };
    try {
      const value = await run(ctx);
      await made.settle();
      await flush();
      if (value === null || typeof value !== "object" || Array.isArray(value)) {
        return { op: "error", kind: "output", message: "run(ctx) must return one JSON object" };
      }
      const encoded = JSON.stringify(value);
      if (new TextEncoder().encode(encoded).length > 2000000) {
        return { op: "error", kind: "output", message: "the returned object must be JSON under 2000000 bytes" };
      }
      return { op: "done", output: value };
    } catch (e) {
      try { await flush(); } catch (e2) { /* the run is already failing; a log flush must not hide it */ }
      const text = (e && e.message !== undefined) ? ((e.name ? e.name + ": " : "") + e.message) : String(e);
      return { op: "error", kind: failureKind(text), message: text.slice(0, 600) };
    }
  }
}
`;

async function bridgeFetch(url, token, message) {
  const res = await fetch(url, {
    method: "POST",
    redirect: "manual",
    headers: {
      authorization: "Bearer " + token,
      "content-type": "application/json",
    },
    body: JSON.stringify(message),
  });
  if (res.status >= 300 && res.status < 400) throw new Error("bridge redirect is refused");
  return res;
}

export class CallBridge extends WorkerEntrypoint {
  async op(message) {
    const res = await bridgeFetch(this.ctx.props.bridgeUrl, this.ctx.props.bridgeToken, message);
    const text = await res.text();
    if (text.length > 8 * 1024 * 1024) throw new Error("bridge response is over 8 MiB");
    let data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      throw new Error("bridge response is not JSON");
    }
    if (!res.ok) throw new Error((data && data.message) || "bridge rejected the call");
    return data;
  }
}

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

async function authorized(secret, header, body) {
  if (!secret) return false;
  const match = /^Bearer ([0-9]+)\.([0-9a-f]{64})$/.exec(header || "");
  if (!match) return false;
  const exp = Number(match[1]);
  if (!Number.isFinite(exp) || exp * 1000 < Date.now()) return false;
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const prefix = new TextEncoder().encode(match[1] + ".");
  const data = new Uint8Array(prefix.length + body.byteLength);
  data.set(prefix, 0);
  data.set(new Uint8Array(body), prefix.length);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, data));
  const got = hexToBytes(match[2]);
  if (sig.length !== got.length) return false;
  let diff = 0;
  for (let i = 0; i < sig.length; i++) diff |= sig[i] ^ got[i];
  return diff === 0;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname !== "/run" || request.method !== "POST") {
      return new Response("not found", { status: 404 });
    }
    if (!env.AUTH_SECRET) return new Response("unauthorized", { status: 401 });
    const body = await request.arrayBuffer();
    if (body.byteLength > 64 * 1024 * 1024) {
      return Response.json({ op: "error", kind: "protocol", message: "run request is over 64 MiB" }, { status: 413 });
    }
    if (!(await authorized(env.AUTH_SECRET, request.headers.get("authorization"), body))) {
      return new Response("unauthorized", { status: 401 });
    }
    let payload;
    try {
      payload = JSON.parse(new TextDecoder().decode(body));
    } catch (e) {
      return Response.json({ op: "error", kind: "protocol", message: "run body is not JSON" }, { status: 400 });
    }
    if (typeof payload.script !== "string" || typeof payload.bridge_url !== "string" || typeof payload.bridge_token !== "string") {
      return Response.json({ op: "error", kind: "protocol", message: "run body is missing script or bridge" }, { status: 400 });
    }
    let bridge;
    try {
      bridge = new URL(payload.bridge_url);
    } catch (e) {
      return Response.json({ op: "error", kind: "protocol", message: "bridge url is not a url" }, { status: 400 });
    }
    if (bridge.protocol !== "http:" && bridge.protocol !== "https:") {
      return Response.json({ op: "error", kind: "protocol", message: "bridge url is not http" }, { status: 400 });
    }
    let begin;
    try {
      begin = await bridgeFetch(payload.bridge_url, payload.bridge_token, { op: "begin", jti: payload.jti });
    } catch (e) {
      return Response.json({ op: "error", kind: "protocol", message: String(e && e.message || e).slice(0, 600) }, { status: 502 });
    }
    if (!begin.ok) return new Response("bridge rejected the run", { status: 401 });

    const wall = Number(payload.wall_s);
    const cpuMs = Math.max(1, Math.min(300000, Math.round((Number.isFinite(wall) ? wall : 1) * 1000)));
    try {
      const worker = env.LOADER.load({
        compatibilityDate: COMPAT,
        mainModule: "guest.js",
        modules: {
          "guest.js": GUEST,
          "user.js": payload.script,
        },
        globalOutbound: null,
        limits: { cpuMs: cpuMs, subRequests: 1000 },
        env: {
          BRIDGE: ctx.exports.CallBridge({ props: { bridgeUrl: payload.bridge_url, bridgeToken: payload.bridge_token } }),
        },
      });
      const result = await worker.getEntrypoint().run({ inputs: payload.inputs || {}, data: payload.data == null ? null : payload.data });
      return Response.json(result);
    } catch (e) {
      const text = String(e && (e.message || e) || e);
      const folded = text.toLowerCase();
      const kind = folded.indexOf("memory") !== -1 ? "memory"
        : (folded.indexOf("cpu") !== -1 || folded.indexOf("time limit") !== -1) ? "timeout"
        : "script";
      return Response.json({ op: "error", kind: kind, message: text.slice(0, 600) });
    }
  },
};
