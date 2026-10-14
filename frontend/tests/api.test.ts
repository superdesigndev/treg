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
