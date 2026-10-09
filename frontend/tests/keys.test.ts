import { expect, test } from 'vitest'
import keys from '../src/state/keys.js'

const spendOf = (apiKeySpend: unknown, id: number) => keys.keySpend.call({ apiKeySpend }, { id })

test('a key shows nothing until spend has loaded', () => {
  expect(spendOf(null, 7)).toBeNull()
})

test('a key with no billed calls reads as zero, not as missing', () => {
  const spend = { keys: [{ id: 3, spend_micro: 4500, calls: 2 }], unattributed: null }
  expect(spendOf(spend, 3)).toEqual({ id: 3, spend_micro: 4500, calls: 2 })
  expect(spendOf(spend, 7)).toEqual({ id: 7, spend_micro: 0, calls: 0 })
})
