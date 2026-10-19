import { afterEach, expect, test, vi } from 'vitest'
import { ApiError, requestJson } from '../src/api'

afterEach(() => vi.unstubAllGlobals())

test('cookie and team headers accompany a JSON request', async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ ok: true }))
  vi.stubGlobal('fetch', fetcher)
  expect(await requestJson('/tools', {}, { 'X-Treg-Org': 'team' }, vi.fn(), () => true)).toEqual({ ok: true })
  expect(fetcher).toHaveBeenCalledWith('/tools', expect.objectContaining({
    credentials: 'include', headers: { 'X-Treg-Org': 'team' },
  }))
})

test('a WAF HTML rejection retries a Unicode body once with base64', async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response('<html>blocked</html>', { status: 403, headers: { 'content-type': 'text/html' } }))
    .mockResolvedValueOnce(Response.json({ ok: true }))
  vi.stubGlobal('fetch', fetcher)
  const body = JSON.stringify({ recipe: '你好 🌍' })
  await requestJson('/skills', { method: 'POST', body }, { 'X-Treg-Token': 'test' }, vi.fn(), () => false)
  expect(fetcher).toHaveBeenCalledTimes(2)
  const retry = fetcher.mock.calls[1]![1]
  expect(Buffer.from(retry.body, 'base64').toString()).toBe(body)
  expect(retry.headers).toMatchObject({ 'X-Treg-Token': 'test', 'X-Treg-Body-Encoding': 'base64' })
})

test('an upstream JSON refusal is never retried', async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ detail: 'forbidden' }, { status: 403 }))
  vi.stubGlobal('fetch', fetcher)
  await expect(requestJson('/skills', { method: 'POST', body: '{}' }, {}, vi.fn(), () => false))
    .rejects.toMatchObject({ status: 403, detail: 'forbidden' })
  expect(fetcher).toHaveBeenCalledTimes(1)
})

test('a structured refusal retains recovery fields and its legacy detail', async () => {
  const body = {
    detail: 'Table leads is bound by two installs and cannot be deleted.',
    code: 'table_bound',
    hint: 'Remove or rebind the installs first.',
    installs: ['lead-emails', 'leads.email'],
    recovery: { metadata_revision: 4 },
  }
  const fetcher = vi.fn().mockResolvedValue(Response.json(body, { status: 409 }))
  vi.stubGlobal('fetch', fetcher)
  await expect(requestJson('/api/workbench/tables/leads', { method: 'DELETE' }, {}, vi.fn(), () => false))
    .rejects.toMatchObject({ status: 409, detail: body.detail, message: body.detail, body })
  expect(fetcher).toHaveBeenCalledTimes(1)
})

test.each([
  { error: 'manifest_invalid', field: 'name', rule: 'A name is required' },
  [{ loc: ['body', 'name'], msg: 'Field required', type: 'missing' }],
])('a legacy structured detail remains available without changing its message', async (detail) => {
  const body = { detail }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(body, { status: 422 })))
  await expect(requestJson('/hub/tools', { method: 'POST', body: '{}' }, {}, vi.fn(), () => false))
    .rejects.toMatchObject({ status: 422, detail, message: 'HTTP 422', body })
})

test('a non-JSON error still reports its HTTP status', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('Bad gateway', { status: 502 })))
  await expect(requestJson('/tools', {}, {}, vi.fn(), () => false))
    .rejects.toMatchObject({ status: 502, detail: undefined, message: 'HTTP 502', body: null })
})

test('login 401 is an ordinary error, not a reload loop', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ detail: 'expired' }, { status: 401 })))
  const expired = vi.fn()
  await expect(requestJson('/auth/email/verify', {}, {}, expired, () => true)).rejects.toBeInstanceOf(ApiError)
  expect(expired).not.toHaveBeenCalled()
})

test('session expiry reloads once without returning a successful result', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 401 })))
  const expired = vi.fn(), settled = vi.fn()
  void requestJson('/tools', {}, {}, expired, () => true).then(settled, settled)
  await vi.waitFor(() => expect(expired).toHaveBeenCalledTimes(1))
  expect(settled).not.toHaveBeenCalled()
})
