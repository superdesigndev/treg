import { expect, test } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { compileTemplate, parse } from '@vue/compiler-sfc'
import { readFileSync } from 'node:fs'
import * as Vue from 'vue'

// Render the real ToolDrawer template against a stand-in dashboard: every name it reads answers
// something harmless, and the drawer's own state (drawerEp, drawerNext, providerPaused) is set.
function renderDrawer(state: Record<string, any>, file = '../src/components/ToolDrawer.vue') {
  const { descriptor } = parse(readFileSync(new URL(file, import.meta.url), 'utf8'))
  const { code } = compileTemplate({ source: descriptor.template!.content, filename: 'ToolDrawer.vue', id: 'td',
    compilerOptions: { mode: 'function' } })
  const render = new Function('Vue', code)(Vue)
  const ctx = new Proxy(state, {
    has: () => true,
    get: (t, k) => (k in t ? t[k as string] : typeof k === 'symbol' ? undefined : () => ''),
  })
  const app = createSSRApp({ render: () => render.call(ctx, ctx, [], {}, ctx, {}, ctx), components: { ProviderLogo: { render: () => h('i') } } })
  app.component('ProviderLogo', { render: () => h('i') })
  return renderToString(app)
}

const ep = { id: 'board.jobs', provider: 'board', provider_display: 'Board', call_template: 'treg call board.jobs' }
const base = { drawerEp: ep, drawerFacts: '', platCopied: '', drawerTab: 'params', drawerIds: [], drawerReviews: null }

test('the drawer renders for a public provider this server has not enabled', async () => {
  const html = await renderDrawer({ ...base, drawerNext: 'off', providerPaused: () => null })
  expect(html).toContain('This server has not enabled Board')
  // a team whose own registered tool serves it can still hand the line to its agent; nothing to try here
  expect(html).toContain('Copy for your agent')
  expect(html).not.toContain('Try it')
})

test('the drawer still renders a paused provider and a callable one', async () => {
  expect(await renderDrawer({ ...base, drawerNext: 'paused', providerPaused: () => ({ message: 'Board is paused.' }) }))
    .toContain('Board is paused.')
  expect(await renderDrawer({ ...base, drawerNext: 'copy', providerPaused: () => null })).toContain('Copy for your agent')
})

test('the paused branch never throws when the provider has no pause message', async () => {
  await expect(renderDrawer({ ...base, drawerNext: 'paused', providerPaused: () => null })).resolves.toBeTypeOf('string')
})

test('the Try dialog never says a credential is injected for a keyless tool', async () => {
  const epTry = { id: 'board.jobs', provider: 'board', platform_auth: 'anonymous', call_template: 'treg call board.jobs' }
  const html = await renderDrawer({ epTry, epTryAccess: { tier: 'none', detail: 'board.jobs is a keyless public API that this server has not enabled' },
    epTryTab: 'api', mkOauth: () => false, tryUnavailableMessage: () => 'refused', tryKeyless: () => true }, '../src/dialogs/TryEndpointDialog.vue')
  expect(html).not.toContain('injects the credential')
  expect(html).not.toContain('the key is injected')
})

test('the Try dialog says the key is injected when the team\'s own key serves a keyless row', async () => {
  const epTry = { id: 'f.tickers', provider: 'f', platform_auth: 'anonymous', call_template: 'treg call f.tickers' }
  const html = await renderDrawer({ epTry, epTryAccess: { tier: 'credential' }, epTryTab: 'cli', mkOauth: () => false,
    tryUnavailableMessage: () => '', tryKeyless: () => false }, '../src/dialogs/TryEndpointDialog.vue')
  expect(html).not.toContain('no provider key is needed')
})
