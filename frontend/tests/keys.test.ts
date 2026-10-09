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

import format from '../src/state/format.js'

const label = (k: object) => format.keyLabel.call({ short: format.short }, k)

test('a key is named by what it is, then whose', () => {
  expect(label({ name: 'Laptop', identity: 'dev@example.com', assigned_type: 'human' })).toBe('Laptop · dev')
  expect(label({ name: 'Agent key', assigned_name: 'scout', identity: 'agent-team-scout@agents.example',
    created_by: 'dev@example.com', assigned_type: 'agent' })).toBe('scout · dev')
})
