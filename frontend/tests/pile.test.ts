import { afterEach, expect, test, vi } from 'vitest'
import { Pile } from '../src/state/pile'

afterEach(() => { vi.unstubAllGlobals() })

test('a destroyed pile never starts its animation again', () => {
  const frame = vi.fn(() => 1)
  vi.stubGlobal('requestAnimationFrame', frame)
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
  const pile = new Pile(() => {})
  pile.bounds(400, 400)
  pile.destroy()
  pile.add('late-tile')
  expect(frame).not.toHaveBeenCalled()
})
