/** Same-origin JSON transport. Credentials and team selection are supplied by the session. */
export interface RequestOptions extends Omit<RequestInit, 'headers'> {
  headers?: Record<string, string>
  encode?: boolean
}

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown = undefined) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`)
  }
}

function encodeBody(body: string): string {
  const bytes = new TextEncoder().encode(body)
  // Avoid argument spreading: imported skill folders can exceed the engine's argument limit.
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
}

export async function requestJson<T = unknown>(
  path: string,
  opts: RequestOptions,
  headers: Record<string, string>,
  expired: () => void,
  sessionMode: () => boolean,
): Promise<T> {
  const { encode, ...request } = opts
  const init: RequestInit = {
    credentials: 'include',
    ...request,
    headers: { ...headers, ...opts.headers },
  }
  if (encode && typeof opts.body === 'string') {
    init.body = encodeBody(opts.body)
    init.headers = { ...headers, ...opts.headers, 'X-Treg-Body-Encoding': 'base64' }
  }
  let response = await fetch(path, init)
  if (response.status === 403 && typeof opts.body === 'string' && !encode &&
      response.headers.get('content-type')?.includes('html')) {
    try {
      response = await fetch(path, {
        ...init,
        body: encodeBody(opts.body),
        headers: { ...headers, ...opts.headers, 'X-Treg-Body-Encoding': 'base64' },
      })
    } catch {
      throw new ApiError(403, 'The edge blocked this request and the encoded retry failed - try a smaller folder')
    }
  }
  if (response.status === 401 && sessionMode() && !path.startsWith('/auth/')) {
    expired()
    // Navigation replaces the application; do not run downstream success handlers meanwhile.
    return new Promise(() => {})
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new ApiError(response.status, body?.detail)
  }
  return response.json()
}
