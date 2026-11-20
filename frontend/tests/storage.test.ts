import { afterEach, expect, test, vi } from 'vitest'
import { storageGet, storageRemove, storageSet } from '../src/state/storage.js'

afterEach(() => { vi.unstubAllGlobals() })

test('storage that throws reads as empty and drops writes', () => {
  vi.stubGlobal('window', { get localStorage(): Storage { throw new DOMException('blocked', 'SecurityError') } })
  expect(storageGet('k')).toBeNull()
  expect(() => { storageSet('k', 'v'); storageRemove('k') }).not.toThrow()
})

test('storage that works reads back what was written', () => {
  const data = new Map<string, string>()
  vi.stubGlobal('window', { localStorage: {
    getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => { data.set(k, v) },
    removeItem: (k: string) => { data.delete(k) },
  } })
  storageSet('k', 'v')
  expect(storageGet('k')).toBe('v')
  storageRemove('k')
  expect(storageGet('k')).toBeNull()
})
