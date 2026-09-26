"""The hub's script sandbox — the CHILD process (docs/HUB-DECISIONS.md round 2).

Runs one maker's `run.js` inside QuickJS, an embedded JavaScript engine with no network, no file
system, no `require`, no `process`, no timers. The only road out is `ctx.call`, which crosses
to the parent over stdin/stdout as one JSON line and comes back as one JSON line; the parent runs
the call through treg's own call path. This file imports nothing from the server: it is spawned
with a scrubbed environment and must stay that small.

Protocol, JSON lines, one per message:
  parent → child   {"op": "run", "script": "...", "inputs": {...}, "data": [rows] | null, "memory_mb": 64, "wall_s": 120}
  child  → parent  {"op": "call", "id": 1, "target": "...", "opts": {...}}
  parent → child   {"op": "result", "id": 1, "status": 200, "headers": {...}, "json": ..., "text": "..."}
                   {"op": "refused", "id": 1, "error": "..."}      (the script sees a thrown Error)
  child  → parent  {"op": "log", "text": "..."}
  child  → parent  {"op": "charge", "usd": 0.03, "label": "vendor-b"}   (an own-key step's cost, billed to the caller)
  child  → parent  {"op": "done", "output": {...}} | {"op": "error", "kind": "...", "message": "..."}

`ctx.call` returns a promise and does NOT block the engine: the child sends the call, keeps
pumping the engine's job queue, and resolves the promise when the parent's reply for that id
arrives, in whatever order replies come. So five calls in one `Promise.all` are five calls in
flight at once (the parent runs them four at a time, like the JSON road). A script that awaits
one call at a time behaves exactly as before.
"""

from __future__ import annotations

import json
import re
import sys

MAX_LOG_LINES = 50
MAX_LOG_CHARS = 2000
MAX_OUTPUT_BYTES = 2_000_000
_EXPORT = re.compile(r"^\s*export\s+default\s+", re.M)

PRELUDE = """
globalThis.__logs = [];
// ctx.csv(text): RFC 4180 in 30 lines - quotes, doubled quotes, commas and newlines inside
// quotes, CRLF. The first row is the header; every other row becomes an object keyed by it.
function __csv(text) {
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
globalThis.__pending = {};
// The child calls this with each reply line from the parent; it settles the promise for that id.
globalThis.__settle = function (packed) {
  const reply = JSON.parse(packed);
  const p = globalThis.__pending[reply.id];
  if (!p) return;
  delete globalThis.__pending[reply.id];
  if (reply.op === "refused") { p.reject(new Error(reply.error)); return; }
  p.resolve({ status: reply.status, headers: reply.headers, json: reply.json, text: reply.text, truncated: !!reply.truncated, timed_out: !!reply.timed_out, cost_usd: reply.cost_usd || 0 });
};
globalThis.ctx = {
  inputs: JSON.parse(__inputs_json),
  data: JSON.parse(__data_json),
  call: function (target, opts) {
    return new Promise((resolve, reject) => {
      const id = __bridge_send(JSON.stringify([String(target), opts || {}]));
      globalThis.__pending[id] = { resolve, reject };
    });
  },
  csv: __csv,
  log: function (text) { __bridge_log(String(text)); },
  // ctx.charge(usd, label): the script's price, one line at a time (a fee, per result, a margin
  // on ctx.call's cost_usd, a vendor's cost). The sum is capped by pricing.max_price_usd, the
  // amount the caller saw before the run; the parent refuses more.
  charge: function (usd, label) { __bridge_charge(JSON.stringify([Number(usd), String(label === undefined ? "" : label)])); },
};
"""


def _emit(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _read() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def main() -> int:
    import quickjs  # the one dependency; server extra

    start = _read()
    if not start or start.get("op") != "run":
        _emit({"op": "error", "kind": "protocol", "message": "expected a run message"})
        return 2
    script = _EXPORT.sub("globalThis.__run = ", start["script"], count=1)
    if "globalThis.__run = " not in script:
        _emit({"op": "error", "kind": "script", "message": "run.js must `export default async function run(ctx)`"})
        return 2

    ctx = quickjs.Context()
    ctx.set_memory_limit(int(start.get("memory_mb", 64)) * 1024 * 1024)
    # No engine time limit: quickjs refuses to call into Python (the bridge) while one is set.
    # The wall clock is the PARENT's: it kills this whole process group at `wall_s`, and the
    # CPU rlimit is the belt under that. An endless loop therefore ends as `timeout` upstairs.
    ctx.set_max_stack_size(1024 * 1024)
    call_id = 0
    logs = 0

    in_flight = 0

    def bridge_send(packed: str) -> int:
        """Send one call and return its id at once. The reply comes back through `__settle` from
        the run loop below, so the engine never blocks on the network."""
        nonlocal call_id, in_flight
        call_id += 1
        in_flight += 1
        target, opts = json.loads(packed)
        _emit({"op": "call", "id": call_id, "target": target, "opts": opts})
        return call_id

    def bridge_log(text: str) -> None:
        nonlocal logs
        if logs < MAX_LOG_LINES:
            logs += 1
            _emit({"op": "log", "text": text[:MAX_LOG_CHARS]})

    def bridge_charge(packed: str) -> None:
        usd, label = json.loads(packed)
        _emit({"op": "charge", "usd": usd, "label": str(label)[:80]})

    ctx.add_callable("__bridge_send", bridge_send)
    ctx.add_callable("__bridge_log", bridge_log)
    ctx.add_callable("__bridge_charge", bridge_charge)
    ctx.set("__inputs_json", json.dumps(start.get("inputs", {}), ensure_ascii=False))
    ctx.set("__data_json", json.dumps(start.get("data"), ensure_ascii=False))   # the uploaded CSV's rows, or null
    try:
        ctx.eval(PRELUDE)
        ctx.eval(script)
        ctx.eval("""
globalThis.__state = "pending"; globalThis.__out = null; globalThis.__err = null;
Promise.resolve().then(() => globalThis.__run(globalThis.ctx))
  .then(v => { globalThis.__out = JSON.stringify(v === undefined ? null : v); globalThis.__state = "done"; })
  .catch(e => { globalThis.__err = (e === null || e === undefined) ? "InternalError: out of memory"
                                   : (e && e.message !== undefined ? (e.name + ": " + e.message) : String(e))
                                   + (e && e.stack ? "\\n" + e.stack : ""); globalThis.__state = "error"; });
""")
        # Drain the job queue until the promise settles. When no job is runnable but calls are in
        # flight, block on the next reply line from the parent and settle that call's promise;
        # the settled promise queues new jobs and the loop goes on. Replies may arrive in any order.
        while ctx.eval("globalThis.__state") == "pending":
            if ctx.execute_pending_job():
                continue
            if in_flight <= 0:
                break
            reply = _read()
            if reply is None:
                reply = {"op": "refused", "id": 0, "error": "the runner went away"}
                for pid in json.loads(ctx.eval("JSON.stringify(Object.keys(globalThis.__pending))")):
                    ctx.eval(f"globalThis.__settle({json.dumps(json.dumps({**reply, 'id': int(pid)}))})")
                in_flight = 0
                continue
            in_flight -= 1
            ctx.eval(f"globalThis.__settle({json.dumps(json.dumps(reply, ensure_ascii=False))})")
        state = ctx.eval("globalThis.__state")
    except Exception as exc:  # noqa: BLE001 — every engine failure becomes one honest message
        text = str(exc)
        kind = ("memory" if "out of memory" in text else "timeout" if "interrupted" in text
                else "stack" if "stack overflow" in text else "script")
        _emit({"op": "error", "kind": kind, "message": text[:600]})
        return 1
    if state == "error":
        text = str(ctx.eval("globalThis.__err"))
        kind = ("memory" if "out of memory" in text else "timeout" if "interrupted" in text
                else "stack" if "stack overflow" in text else "script")
        _emit({"op": "error", "kind": kind, "message": text[:600]})
        return 1
    if state != "done":
        _emit({"op": "error", "kind": "script", "message": "run(ctx) never settled (an await that waits on nothing)"})
        return 1
    out = ctx.eval("globalThis.__out")
    try:
        if not isinstance(out, str) or len(out.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError("not a JSON string")
        parsed = json.loads(out)
    except (ValueError, TypeError, AttributeError):
        _emit({"op": "error", "kind": "output", "message": f"the returned object must be JSON under {MAX_OUTPUT_BYTES} bytes"})
        return 1
    _emit({"op": "done", "output": parsed})
    return 0


if __name__ == "__main__":
    sys.exit(main())
