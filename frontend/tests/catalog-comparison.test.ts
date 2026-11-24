import { expect, test } from 'vitest'
import catalogComputed from '../src/state/catalogComputed.js'

const verify = {
  capability: 'people.email.verify', compare: 'email.verify', title: 'Verify an email address', description: 'Verify',
  endpoints: [
    { id: 'treg.people.email.verify', provider: 'treg', kind: 'routed', cost: {} },
    { id: 'enrow.people.email.verify', provider: 'enrow', kind: 'data', cost: { usd: 0.0022 } },
    { id: 'enrow.people.email.verify.get', provider: 'enrow', kind: 'utility', cost: { usd: 0 } },
    { id: 'kitt.people.email.verify', provider: 'kitt', kind: 'data', cost: { usd: 0.0015 } },
  ],
}

const vm: any = {
  platData: { domains: [{ domain: 'contacts', rows: [verify] }] },
  platComparisonSort: { key: 'price', dir: 'asc' }, platAccepts: {}, platObserved: {},
  clip: (t: string) => t, callsOf: () => 0, priceRange: () => '', costUsd: (c: any) => c.usd,
  takesLabel: () => '', worksOf: () => null, usefulOf: () => null,
}
for (const k of ['platRowsAll', 'platComparisons', 'platToolIndex']) {
  Object.defineProperty(vm, k, { get() { return (catalogComputed as any)[k].call(this) } })
}
vm.platCapRow = verify

test('a comparison lists the providers that do the job, never a result poll', () => {
  expect(catalogComputed.platComparisonRows.call(vm).map((r: any) => r.id))
    .toEqual(['kitt.people.email.verify', 'enrow.people.email.verify'])
  expect(vm.platComparisons[0].provN).toBe(2)
})

test('the poll stays reachable under account and setup', () => {
  const poll = vm.platToolIndex.filter((t: any) => t.id === 'enrow.people.email.verify.get')
  expect(poll.map((t: any) => t.mgmt)).toEqual([true])
  expect(vm.platToolIndex.some((t: any) => t.id === 'enrow.people.email.verify')).toBe(false)
})
