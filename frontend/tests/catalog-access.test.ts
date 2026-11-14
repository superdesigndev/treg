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
