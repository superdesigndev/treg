import { expect, test } from 'vitest'
import keys from '../src/state/keys.js'

const spendOf = (apiKeySpend: unknown, id: number) => keys.keySpend.call({ apiKeySpend }, { id })

test('a key shows nothing until spend has loaded and the rollup is ready', () => {
  expect(spendOf(null, 7)).toBeNull()
  expect(spendOf({ ready: false, keys: [], unattributed: null }, 7)).toBeNull()
})

test('a key with no billed charges reads as zero, not as missing', () => {
  const spend = { ready: true, keys: [{ id: 3, spent_micro: 4500, charges: 2 }], unattributed: null }
  expect(spendOf(spend, 3)).toEqual({ id: 3, spent_micro: 4500, charges: 2 })
  expect(spendOf(spend, 7)).toEqual({ id: 7, spent_micro: 0, charges: 0 })
})
