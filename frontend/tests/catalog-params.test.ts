import { expect, test } from 'vitest'
import catalog from '../src/state/catalog.js'

const paramType = (spec: unknown): string => (catalog as any).paramType.call(catalog, spec)

test("a tool drawer names each parameter's type in words", () => {
  expect(paramType({ type: 'string' })).toBe('string')
  expect(paramType({ type: ['boolean', 'string'] })).toBe('boolean or string')
  expect(paramType({ type: 'array', items: { type: 'string' } })).toBe('array of string')
  expect(paramType({ type: 'string', format: 'url' })).toBe('string (url)')
  expect(paramType({})).toBe('')
})
