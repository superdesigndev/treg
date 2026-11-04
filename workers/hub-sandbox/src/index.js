// Loader for one hub script. treg opens a WebSocket to /run and keeps it for the run; the
// script's ctx.call travels back over that socket, so nothing ever connects into treg. The
// dynamic worker has no outbound network and no bindings: its one road out is the bridge object
// passed to run(), which lives in this isolate and holds no key.
import { RpcTarget } from "cloudflare:workers";

const COMPAT = "2026-08-06";
const AUTH_WAIT_MS = 10000;
const GUEST_OPS = new Set(["call", "charge", "logs"]);

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
  async run(payload, bridge) {
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
    const made = makeCtx(bridge, payload.inputs || {}, payload.data == null ? null : payload.data);
    const ctx = made.ctx;
    const flush = async () => {
      if (!ctx.logs.length) return;
      const lines = ctx.logs.splice(0, ctx.logs.length);
      await bridge.op({ op: "logs", lines: lines });
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

class Bridge extends RpcTarget {
  constructor(socket) {
    super();
    this.socket = socket;
    this.next = 1;
    this.waiting = new Map();
    this.closed = false;
  }

  op(message) {
    if (this.closed) return Promise.reject(new Error("the run has ended"));
    if (!message || typeof message !== "object" || !GUEST_OPS.has(message.op)) {
      return Promise.reject(new Error("unknown bridge message"));
    }
    const id = this.next++;
    return new Promise((resolve, reject) => {
      this.waiting.set(id, { resolve, reject });
      try {
        this.socket.send(JSON.stringify({ t: "op", id: id, msg: message }));
      } catch (e) {
        this.waiting.delete(id);
        reject(new Error("the run has ended"));
      }
    });
  }

  answer(frame) {
    const waiter = this.waiting.get(frame.id);
    if (!waiter) return;
    this.waiting.delete(frame.id);
    waiter.resolve(frame.reply);
  }

  end() {
    this.closed = true;
    for (const waiter of this.waiting.values()) waiter.reject(new Error("the run has ended"));
    this.waiting.clear();
  }
}

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

async function authorized(secret, auth, run) {
  if (!secret || typeof auth !== "string" || typeof run !== "string") return false;
  const match = /^([0-9]+)\.([0-9a-f]{64})$/.exec(auth);
  if (!match) return false;
  const exp = Number(match[1]);
  if (!Number.isFinite(exp) || exp * 1000 < Date.now()) return false;
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const data = new TextEncoder().encode(match[1] + "." + run);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, data));
  const got = hexToBytes(match[2]);
  if (sig.length !== got.length) return false;
  let diff = 0;
  for (let i = 0; i < sig.length; i++) diff |= sig[i] ^ got[i];
  return diff === 0;
}

function failureKind(text) {
  const folded = text.toLowerCase();
  if (folded.indexOf("memory") !== -1) return "memory";
  if (folded.indexOf("cpu") !== -1 || folded.indexOf("time limit") !== -1) return "timeout";
  return "script";
}

async function execute(env, payload, bridge) {
  const wall = Number(payload.wall_s);
  const cpuMs = Math.max(1, Math.min(300000, Math.round((Number.isFinite(wall) ? wall : 1) * 1000)));
  try {
    const worker = env.LOADER.load({
      compatibilityDate: COMPAT,
      mainModule: "guest.js",
      modules: { "guest.js": GUEST, "user.js": payload.script },
      globalOutbound: null,
      limits: { cpuMs: cpuMs, subRequests: 1000 },
      env: {},
    });
    return await worker.getEntrypoint().run(
      { inputs: payload.inputs || {}, data: payload.data == null ? null : payload.data }, bridge);
  } catch (e) {
    const text = String(e && (e.message || e) || e);
    return { op: "error", kind: failureKind(text), message: text.slice(0, 600) };
  }
}

function finish(socket, frame) {
  try {
    socket.send(JSON.stringify(frame));
    socket.close(1000, "done");
  } catch (e) { /* treg already left; its wall clock decided the run */ }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/run") return new Response("not found", { status: 404 });
    if (request.headers.get("upgrade") !== "websocket") return new Response("websocket only", { status: 426 });
    if (!env.AUTH_SECRET) return new Response("unauthorized", { status: 401 });

    const [client, socket] = Object.values(new WebSocketPair());
    socket.accept();
    let bridge = null;
    const waitForAuth = setTimeout(() => {
      if (!bridge) finish(socket, { t: "end", result: { op: "error", kind: "protocol", message: "no run arrived" } });
    }, AUTH_WAIT_MS);

    socket.addEventListener("close", () => { if (bridge) bridge.end(); });
    socket.addEventListener("message", async (event) => {
      let frame;
      try {
        frame = JSON.parse(typeof event.data === "string" ? event.data : new TextDecoder().decode(event.data));
      } catch (e) {
        finish(socket, { t: "end", result: { op: "error", kind: "protocol", message: "frame is not JSON" } });
        return;
      }
      if (bridge) {
        if (frame && frame.t === "reply") bridge.answer(frame);
        return;
      }
      clearTimeout(waitForAuth);
      bridge = new Bridge(socket);
      if (!frame || !(await authorized(env.AUTH_SECRET, frame.auth, frame.run))) {
        bridge.end();
        finish(socket, { t: "end", result: { op: "error", kind: "protocol", message: "unauthorized" } });
        return;
      }
      let payload;
      try {
        payload = JSON.parse(frame.run);
      } catch (e) {
        payload = null;
      }
      if (!payload || typeof payload.script !== "string") {
        bridge.end();
        finish(socket, { t: "end", result: { op: "error", kind: "protocol", message: "run is missing its script" } });
        return;
      }
      const result = await execute(env, payload, bridge);
      bridge.end();
      finish(socket, { t: "end", result: result });
    });

    return new Response(null, { status: 101, webSocket: client });
  },
};
