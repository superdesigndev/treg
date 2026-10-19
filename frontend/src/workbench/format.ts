export function usd(micro: number | null | undefined): string {
  if (micro == null) return ''
  if (micro === 0) return '$0'
  if (micro < 10000) return `$${(micro / 1e6).toFixed(4)}`
  return `$${(micro / 1e6).toFixed(2)}`
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return ''
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 90) return 'just now'
  if (s < 5400) return `${Math.round(s / 60)} min ago`
  if (s < 129600) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}

export function when(iso: string | null | undefined): string {
  if (!iso) return ''
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export function count(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`
}

export function cellText(value: unknown): string {
  if (value == null) return ''
  if (value === true) return 'true'
  if (value === false) return 'false'
  return typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
}

export const RUN_TONE: Record<string, 'live' | 'ok' | 'warn' | 'bad' | 'plain'> = {
  queued: 'plain', running: 'live', stopping: 'warn', finalizing: 'live',
  succeeded: 'ok', partial: 'warn', failed: 'bad', stopped: 'plain',
  active: 'ok', paused: 'warn', archived: 'plain', no_result: 'plain',
}
