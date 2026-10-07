// A hub tool's answer as something a person reads: each declared output field becomes a block whose
// shape follows its value. Everything is plain data rendered as text by Vue; nothing here is HTML.

export type Block =
  | { kind: 'empty', label: string }
  | { kind: 'value', label: string, text: string }
  | { kind: 'link', label: string, href: string }
  | { kind: 'image', label: string, src: string }
  | { kind: 'list', label: string, items: string[] }
  | { kind: 'table', label: string, columns: string[], rows: string[][], raw: Record<string, unknown>[] }
  | { kind: 'section', label: string, blocks: Block[] }
  | { kind: 'json', label: string, text: string }
  | { kind: 'tiles', label: string, items: { label: string, text: string }[] }

const MAX_DEPTH = 3
const IMAGE = /\.(png|jpe?g|gif|webp|svg|avif)(\?|#|$)/i

export function isUrl(v: unknown): v is string {
  return typeof v === 'string' && /^https?:\/\/[^\s]+$/i.test(v)
}

// Only http(s) ever becomes a link or an image: a `javascript:` value stays text.
export function safeHref(v: string): string | null {
  try {
    const u = new URL(v)
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null
  } catch { return null }
}

// A link as a person reads it in a cell: the host and the start of the path, never the query.
export function shortUrl(href: string): string {
  try {
    const u = new URL(href)
    const path = u.pathname === '/' ? '' : u.pathname.length > 28 ? u.pathname.slice(0, 27) + '…' : u.pathname
    return u.host.replace(/^www\./, '') + path
  } catch { return href }
}

export function cell(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString('en-US') : String(v)
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  return JSON.stringify(v)
}

function isEmpty(v: unknown): boolean {
  return v === null || v === undefined || v === '' ||
    (Array.isArray(v) && v.length === 0) ||
    (typeof v === 'object' && !Array.isArray(v) && Object.keys(v as object).length === 0)
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

export function columnsOf(rows: Record<string, unknown>[]): string[] {
  const seen: string[] = []
  for (const r of rows) for (const k of Object.keys(r)) if (!seen.includes(k)) seen.push(k)
  return seen
}

export function block(label: string, v: unknown, depth = 0): Block {
  if (isEmpty(v)) return { kind: 'empty', label }
  if (isUrl(v)) {
    const href = safeHref(v)
    if (href) return IMAGE.test(href) ? { kind: 'image', label, src: href } : { kind: 'link', label, href }
  }
  if (typeof v !== 'object') return { kind: 'value', label, text: cell(v) }
  if (Array.isArray(v)) {
    if (v.every(isRecord)) {
      const raw = v as Record<string, unknown>[]
      const columns = columnsOf(raw)
      return { kind: 'table', label, columns, rows: raw.map(r => columns.map(c => cell(r[c]))), raw }
    }
    if (v.every(x => x === null || typeof x !== 'object')) return { kind: 'list', label, items: v.map(cell) }
    return { kind: 'json', label, text: JSON.stringify(v, null, 2) }
  }
  if (depth >= MAX_DEPTH) return { kind: 'json', label, text: JSON.stringify(v, null, 2) }
  return { kind: 'section', label, blocks: tiles(Object.entries(v as object).map(([k, x]) => block(k, x, depth + 1))) }
}

// Runs of short single values (a count, a name, yes/no) read best side by side as tiles; everything
// else keeps its own row. Long text stays a row of its own.
export function tiles(list: Block[]): Block[] {
  const out: Block[] = []
  let run: { label: string, text: string }[] = []
  const flush = () => {
    if (run.length > 1) out.push({ kind: 'tiles', label: run.map(r => r.label).join(','), items: run })
    else if (run.length === 1) out.push({ kind: 'value', label: run[0].label, text: run[0].text })
    run = []
  }
  for (const b of list) {
    if ((b.kind === 'value' && b.text.length <= 40) || b.kind === 'empty') run.push({ label: b.label, text: b.kind === 'value' ? b.text : '—' })
    else { flush(); out.push(b) }
  }
  flush()
  return out
}

// The declared fields first, in their declared order; anything else the answer carries after them.
export function blocks(output: unknown, declared: string[]): Block[] {
  if (!isRecord(output)) return [block('Result', output)]
  const names = [...declared.filter(k => k in output), ...Object.keys(output).filter(k => !declared.includes(k))]
  return tiles(names.map(k => block(k, output[k])))
}

function csvCell(s: string): string {
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

export function toCsv(columns: string[], rows: string[][]): string {
  return [columns, ...rows].map(r => r.map(csvCell).join(',')).join('\n') + '\n'
}

export function download(name: string, text: string, type: string): void {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
