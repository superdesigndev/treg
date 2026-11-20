import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// src/treg/web/captcha.js runs on every sign-in page; here it runs against a fake DOM and a fake
// Turnstile so the token rules (single use, freshness, bounded waiting) are pinned down.
const SOURCE = readFileSync(resolve(__dirname, '../../src/treg/web/captcha.js'), 'utf8')

type Widget = { opts: any, removed: boolean }

function page(siteKey: string, { turnstile = true, theme = '', inDialog = false } = {}) {
  const widgets: Widget[] = []
  const hosts: string[] = []
  const boxes: any[] = []
  const focus: ((e: any) => void)[] = []
  const element = () => {
    const el: any = { attrs: {} as Record<string, string>, style: {}, isConnected: true }
    el.setAttribute = (k: string, v: string) => { el.attrs[k] = v }
    el.getAttribute = (k: string) => el.attrs[k] ?? null
    el.remove = () => { el.isConnected = false }
    el.closest = () => null
    el.getBoundingClientRect = () => ({ left: 0, width: 0, bottom: 0, height: 0 })
    el.children = [] as any[]
    el.appendChild = (c: any) => { el.children.push(c) }
    return el
  }
  const scripts: any[] = []
  const dialog = { appendChild: (b: any) => { hosts.push('dialog'); boxes.push(b) } }
  const document = {
    readyState: 'complete',
    createElement: element,
    documentElement: { getAttribute: (k: string) => (k === 'data-theme' ? theme || null : null) },
    head: { appendChild: (s: any) => scripts.push(s) },
    body: { appendChild: (b: any) => { hosts.push('body'); boxes.push(b) } },
    addEventListener: (type: string, fn: any) => { if (type === 'focusin') focus.push(fn) },
  }
  const fake = {
    render: (_el: any, opts: any) => { widgets.push({ opts, removed: false }); return widgets.length - 1 },
    remove: (id: number) => { widgets[id].removed = true },
  }
  const window: any = { innerHeight: 800, console: { warn: () => {} } }
  if (turnstile) window.turnstile = fake
  Object.assign(window, { window, document, setTimeout, clearTimeout, Date, Promise })
  runInNewContext(SOURCE.replace('"__TREG_TURNSTILE_SITE_KEY__"', JSON.stringify(siteKey)), window)
  const field = element()
  field.setAttribute('data-captcha', 'email_start')
  field.closest = (sel: string) => (inDialog && sel === 'dialog[open]' ? dialog : null)
  return {
    captcha: window.tregCaptcha as { token: (a: string) => Promise<string>, enabled: boolean },
    widgets, scripts, hosts, boxes, window,
    focusSignIn: () => focus.forEach(fn => fn({ target: field })),
    focusOther: () => { const other = element(); focus.forEach(fn => fn({ target: other })) },
    solve: (i: number, token: string) => widgets[i].opts.callback(token),
  }
}


describe('captcha.js', () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] }) })
  afterEach(() => { vi.useRealTimers() })

  it('is inert without a site key', async () => {
    const p = page('')
    expect(p.captcha.enabled).toBe(false)
    p.focusSignIn()
    await expect(p.captcha.token('email_start')).resolves.toBe('')
    expect(p.widgets).toHaveLength(0)
  })

  it('prefetches only on fields marked for sign-in', async () => {
    const p = page('key')
    p.focusOther()
    await vi.advanceTimersByTimeAsync(0)
    expect(p.widgets).toHaveLength(0)
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    expect(p.widgets).toHaveLength(1)
    expect(p.widgets[0].opts.action).toBe('email_start')
    expect(p.widgets[0].opts.appearance).toBe('interaction-only')
  })

  it('hands a prefetched token out once, then solves a new one', async () => {
    const p = page('key')
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    p.solve(0, 'tok-1')
    await expect(p.captcha.token('email_start')).resolves.toBe('tok-1')
    const second = p.captcha.token('email_start')
    await vi.advanceTimersByTimeAsync(0)
    expect(p.widgets).toHaveLength(2)
    p.solve(1, 'tok-2')
    await expect(second).resolves.toBe('tok-2')
  })

  it('gives two concurrent callers two checks, never one token twice', async () => {
    const p = page('key')
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    const a = p.captcha.token('email_start')
    const b = p.captcha.token('email_start')
    await vi.advanceTimersByTimeAsync(0)
    expect(p.widgets).toHaveLength(2)
    p.solve(0, 'tok-a'); p.solve(1, 'tok-b')
    expect(new Set([await a, await b])).toEqual(new Set(['tok-a', 'tok-b']))
  })

  it('stops waiting after 30s but keeps the widget, and the next click gets its token', async () => {
    const p = page('key')
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    const first = p.captcha.token('email_start')
    await vi.advanceTimersByTimeAsync(30_000)
    await expect(first).resolves.toBe('!pending')
    expect(p.widgets[0].removed).toBe(false)
    p.solve(0, 'late')
    await expect(p.captcha.token('email_start')).resolves.toBe('late')
    expect(p.widgets).toHaveLength(1)
  })

  it('replaces a stale check instead of floating a second widget', async () => {
    const p = page('key')
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(241_000)
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    expect(p.widgets).toHaveLength(2)
    expect(p.widgets[0].removed).toBe(true)
    expect(p.widgets[1].removed).toBe(false)
  })

  it('says !blocked when the Turnstile script cannot load, and retries next time', async () => {
    const p = page('key', { turnstile: false })
    const first = p.captcha.token('email_start')
    p.scripts.at(-1).onerror()
    await expect(first).resolves.toBe('!blocked')
    p.captcha.token('email_start')
    expect(p.scripts.length).toBeGreaterThan(1)
  })

  it('says !error when the widget fails, and the failed check is not reused', async () => {
    const p = page('key')
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    p.widgets[0].opts['error-callback'](110200)
    await vi.advanceTimersByTimeAsync(0)
    const next = p.captcha.token('email_start')
    await vi.advanceTimersByTimeAsync(0)
    expect(p.widgets).toHaveLength(2)
    p.solve(1, 'fresh')
    await expect(next).resolves.toBe('fresh')
  })

  it('mounts the widget inside an open modal dialog, else on the body', async () => {
    const inside = page('key', { inDialog: true })
    inside.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    expect(inside.hosts).toEqual(['dialog'])
    const outside = page('key')
    outside.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    expect(outside.hosts).toEqual(['body'])
  })

  it('refocusing the field while a submit waits never floats a second widget', async () => {
    const p = page('key')
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    const first = p.captcha.token('email_start')
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(30_000)
    await expect(first).resolves.toBe('!pending')
    expect(p.widgets).toHaveLength(1)
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    expect(p.widgets).toHaveLength(1)
    p.solve(0, 'late')
    await expect(p.captcha.token('email_start')).resolves.toBe('late')
  })

  it('cancels the older check when a second submit replaced it', async () => {
    const p = page('key')
    const a = p.captcha.token('email_start')
    const b = p.captcha.token('email_start')
    await vi.advanceTimersByTimeAsync(30_000)
    await expect(a).resolves.toBe('!pending')
    await expect(b).resolves.toBe('!pending')
    expect(p.widgets[0].removed).toBe(true)
    expect(p.widgets[1].removed).toBe(false)
  })

  it('reports a script that never loads as !blocked after 10s, not as pending', async () => {
    const p = page('key', { turnstile: false })
    const first = p.captcha.token('email_start')
    await vi.advanceTimersByTimeAsync(10_000)
    await expect(first).resolves.toBe('!blocked')
    expect(p.widgets).toHaveLength(0)
  })

  it('a widget that times out at Cloudflare is !error and not reused', async () => {
    const p = page('key')
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    p.widgets[0].opts['timeout-callback']()
    await vi.advanceTimersByTimeAsync(0)
    const next = p.captcha.token('email_start')
    await vi.advanceTimersByTimeAsync(0)
    expect(p.widgets).toHaveLength(2)
    p.solve(1, 'again')
    await expect(next).resolves.toBe('again')
  })

  it('follows the page theme, else the system one', async () => {
    const dark = page('key', { theme: 'dark' })
    dark.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    expect(dark.widgets[0].opts.theme).toBe('dark')
    const plain = page('key')
    plain.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    expect(plain.widgets[0].opts.theme).toBe('auto')
  })

  it('explains the check only when Cloudflare wants a click', async () => {
    const p = page('key')
    p.focusSignIn()
    await vi.advanceTimersByTimeAsync(0)
    const caption = p.boxes[0].children[0]
    expect(caption.textContent).toBe('One quick check before we email your code')
    expect(caption.style.cssText).toContain('display:none')
    p.widgets[0].opts['before-interactive-callback']()
    expect(caption.style.display).toBe('block')
  })
})
