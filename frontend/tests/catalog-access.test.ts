import { expect, test } from 'vitest'
import catalog from '../src/state/catalog.js'
import catalogComputed from '../src/state/catalogComputed.js'

const label = (e: object, providers: object[]) =>
  catalog.endpointAccessLabel.call({ providers, platData: null }, e)

test('a keyless public row is free with no key, never treg\'s key', () => {
  const e = { id: 'board.jobs', provider: 'board', platform_auth: 'anonymous', platform_eligible: true }
  expect(label(e, [{ service: 'board', auth_kind: 'public' }])).toBe('Free, no key')
  // a key provider's keyless row too: it runs with no key at all
  expect(label(e, [{ service: 'board', auth_kind: 'key' }])).toBe('Free, no key')
  const vm = { drawerEp: e, endpointAccessLabel: () => 'Free, no key', verdictDate: (d: string) => d }
  expect(catalogComputed.drawerFacts.call(vm)).toContain('free, no key needed')
  expect(catalogComputed.drawerFacts.call(vm)).not.toContain("treg's key")
})

test('keyed rows keep their labels', () => {
  const e = { id: 'p.x', provider: 'p', platform_eligible: true }
  expect(label(e, [{ service: 'p', auth_kind: 'key' }])).toBe('Platform + BYOK')
})

test('Try it never tells a keyless public row it needs a key', () => {
  const vm = { mkOauth: () => false }
  const msg = (ep: object, access: object) => catalog.tryUnavailableMessage.call(vm, ep, access)
  const keyless = { id: 'board.jobs', provider: 'board', platform_auth: 'anonymous' }
  expect(msg(keyless, { detail: 'board.jobs is a keyless public API that this server has not enabled' }))
    .toBe('board.jobs is a keyless public API that this server has not enabled')
  expect(msg(keyless, {})).not.toContain('needs a key')
  expect(msg({ id: 'p.x', provider: 'p' }, {})).toContain('needs a key')
})

test('a keyless public provider has a provider page though it is not in the Connect listing', () => {
  const tools = { service: 'board', data: { provider: { service: 'board', display_name: 'Board', auth_kind: 'public' } } }
  const vm = { providers: [{ service: 'other' }], mkService: 'board', mkTools: tools,
    get mkToolData(){ return catalogComputed.mkToolData.call(this) } }
  const p = catalogComputed.mkProvider.call(vm)
  expect(p && p.display_name).toBe('Board')
  expect(p.auth_kind).toBe('public')
  // a provider that is neither listed nor public still has no page (unknown service)
  const unknown = { providers: [{ service: 'other' }], mkService: 'nope',
    mkTools: { service: 'nope', data: { provider: { service: 'nope' } } },
    get mkToolData(){ return catalogComputed.mkToolData.call(this) } }
  expect(catalogComputed.mkProvider.call(unknown)).toBeNull()
})
