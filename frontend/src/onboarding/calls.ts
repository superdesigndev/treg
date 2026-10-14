// The first-task library as the server sends it (application/onboard/tasks.py), and the calls a
// filled-in task makes. `buildCalls` is the same rule as the server's `build_calls`: `parse` names
// groups over the value, a body or query string may name a group (`{value}` is the whole input), and
// a key whose groups are all empty is dropped.

export type LibCall = { endpoint: string; method: string; body: Record<string, unknown>; query: Record<string, unknown> }
export type LibTask = {
  id: string; tpl: string; say: string; slot: string; view: string; parse: string
  example: { value: string; note: string }; unit: string; unit_usd: number | null; calls: LibCall[]
}
export type RankedTask = { id: string; value: string; note: string; example: boolean; score: number | null; recommended: boolean }

const GROUP = /\{(\w+)\}/g

function fill(template: unknown, groups: Record<string, string>): unknown {
  if (typeof template !== 'string') return template
  const names = [...template.matchAll(GROUP)].map((m) => m[1])
  if (names.length && !names.some((n) => groups[n])) return undefined
  return template.replace(GROUP, (_, n) => groups[n] ?? '')
}

function fillAll(obj: Record<string, unknown>, groups: Record<string, string>) {
  const out: Record<string, unknown> = {}
  for (const [k, t] of Object.entries(obj)) {
    const v = fill(t, groups)
    if (v !== undefined) out[k] = v
  }
  return out
}

export function buildCalls(task: LibTask, value: string): LibCall[] | null {
  const v = value.trim()
  if (!v) return null
  let groups: Record<string, string> = { value: v }
  if (task.parse) {
    const m = new RegExp(task.parse).exec(v)
    if (!m) return null
    groups = { value: v, ...Object.fromEntries(Object.entries(m.groups || {}).map(([k, g]) => [k, g || ''])) }
  }
  return task.calls.map((c) => ({ endpoint: c.endpoint, method: c.method, body: fillAll(c.body, groups), query: fillAll(c.query, groups) }))
}

const shellQuote = (s: string) => (/^[\w@%+=:,./-]+$/.test(s) ? s : "'" + s.replace(/'/g, "'\\''") + "'")

// The command an agent (or a person) would type for the same call.
export function cliLine(c: LibCall): string {
  const q = Object.entries(c.query).map(([k, v]) => ' --query ' + shellQuote(`${k}=${v}`)).join('')
  const body = Object.keys(c.body).length ? ' --body ' + shellQuote(JSON.stringify(c.body)) : ''
  return `treg call ${c.endpoint}${q}${body}`
}

export type CallResult = {
  call: LibCall; status: number; ms: number; costMicro: number; servedBy: string
  body: unknown; error: string
}

// One call, exactly as an agent makes it: the team's own `/call/`, metered on its credit. The route
// cap keeps a waterfall from spending more than a few cents of a new team's free credit.
export async function runCall(c: LibCall, headers: Record<string, string>, signal?: AbortSignal): Promise<CallResult> {
  const qs = new URLSearchParams(Object.entries(c.query).map(([k, v]) => [k, String(v)])).toString()
  const opts: RequestInit = {
    method: c.method, credentials: 'include', signal,
    headers: { ...headers, 'X-Treg-Client': 'onboarding', 'X-Treg-Route-Max-Cost': '0.05' },
  }
  if (c.method !== 'GET') {
    opts.body = JSON.stringify(c.body)
    ;(opts.headers as Record<string, string>)['content-type'] = 'application/json'
  }
  const t0 = performance.now()
  try {
    const r = await fetch('/call/' + c.endpoint + (qs ? '?' + qs : ''), opts)
    const text = await r.text()
    let body: unknown = text
    try { body = text ? JSON.parse(text) : null } catch { /* a non-JSON answer stays text */ }
    const err = r.ok ? '' : errorText(r.status, body)
    return {
      call: c, status: r.status, ms: Math.round(performance.now() - t0),
      costMicro: Number(r.headers.get('X-Treg-Cost-Micro') || 0),
      servedBy: r.headers.get('X-Treg-Served-By') || '', body, error: err,
    }
  } catch (e) {
    return { call: c, status: 0, ms: Math.round(performance.now() - t0), costMicro: 0, servedBy: '', body: null,
      error: (e as Error)?.name === 'AbortError' ? 'The call took too long.' : 'The call could not reach treg.' }
  }
}

// A preview's first call (`/app#onboarding-preview`): the same call, made by the server as a house
// call, so a registry with no provider credit of its own still shows a real answer.
export async function runPreviewCall(id: string, c: LibCall, headers: Record<string, string>): Promise<CallResult> {
  const t0 = performance.now()
  try {
    const r = await fetch('/onboarding/preview/' + encodeURIComponent(id) + '/call', {
      method: 'POST', credentials: 'include', headers: { ...headers, 'content-type': 'application/json' },
      body: JSON.stringify(c),
    })
    const d = await r.json().catch(() => ({}))
    const ms = Math.round(performance.now() - t0)
    if (!r.ok) return { call: c, status: r.status, ms, costMicro: 0, servedBy: '', body: null, error: errorText(r.status, d) }
    const status = Number(d.status) || 0
    return { call: c, status, ms, costMicro: Number(d.cost_micro) || 0, servedBy: d.served_by || '', body: d.body,
      error: status >= 200 && status < 300 ? '' : status ? errorText(status, d.body) : 'The call could not reach treg.' }
  } catch {
    return { call: c, status: 0, ms: Math.round(performance.now() - t0), costMicro: 0, servedBy: '', body: null, error: 'The call could not reach treg.' }
  }
}

// What went wrong, for the exception report; the page never shows it.
function errorText(status: number, body: unknown): string {
  const b = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>
  const detail = typeof b.detail === 'string' ? b.detail : typeof b.error === 'string' ? b.error : ''
  return `HTTP ${status}` + (detail ? ': ' + detail.slice(0, 200) : '')
}

// Two significant figures, rounded down: 125 -> 120, 9,990 -> 9,900. "About" never overpromises.
export const floor2 = (x: number) => {
  if (x < 100) return Math.floor(x)
  const p = 10 ** (Math.floor(Math.log10(x)) - 1)
  return Math.floor(x / p) * p
}

export const fmtCost = (micro: number) => (micro === 0 ? 'free' : '$' + (micro / 1e6).toFixed(micro < 10000 ? 4 : 3))
