import { expect, test, vi } from 'vitest'
import { exampleBanners } from '../src/pages/getting-started-art'

vi.stubGlobal('location', { origin: 'http://localhost' })
const { examples } = await import('../src/agent-setup/data')

test('every Try it out card has its banner', () => {
  expect(examples.length).toBeGreaterThan(0)
  expect(examples.filter(ex => !exampleBanners[ex.k]).map(ex => ex.k)).toEqual([])
})
