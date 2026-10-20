import { ApiError } from '../api'

/** Lens mutations never inherit the shared transport's encoded-body retry. A rejected or lost
 * response may follow an accepted paid run; only GET of its preallocated id can resolve that. */
export async function lensRequest<T>(path: string, headers: Record<string, string>, body?: unknown, method = 'GET'): Promise<T> {
  const response = await fetch(path, {
    method, credentials: 'include', headers: { ...headers, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const data = await response.json().catch(() => null)
  if (!response.ok) throw new ApiError(response.status, data?.detail || 'The request could not be confirmed. Check its status before trying again.')
  if (data === null) throw new Error('The server response could not be read. Check its status before trying again.')
  return data as T
}

export function lensError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return 'Your session has expired. Sign in again to continue; your draft is saved on this browser.'
    if (error.status === 403) return 'Your team permissions do not allow this action.'
    if (error.status === 409) return 'This project or quote has changed. Reload the saved project and review a new estimate.'
    if (error.status === 402) return 'Your team needs more credit or a higher spending limit to run this request.'
    if (typeof error.detail === 'string') return error.detail
    if (typeof error.detail === 'object' && error.detail && 'message' in error.detail) return String(error.detail.message)
  }
  return error instanceof Error ? error.message : 'The request could not be completed. Your draft is still here.'
}

export function microUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isSafeInteger(value) || value < 0) return 'Unconfirmed'
  const whole = Math.floor(value / 1_000_000)
  const fraction = String(value % 1_000_000).padStart(6, '0').replace(/0+$/, '').padEnd(2, '0')
  return `$${whole.toLocaleString('en-US')}.${fraction}`
}

export function publicSource(value: string): string | undefined {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined } catch { return undefined }
}
