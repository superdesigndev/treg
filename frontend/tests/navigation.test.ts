import { expect, test } from 'vitest'
import navigation, { parseTabHash } from '../src/state/navigation.js'

test('only Activity and Team addresses carry a tab, and Activity a key id', () => {
  expect(parseTabHash('#activity/usage')).toEqual({ view: 'activity', tab: 'usage', key: '' })
  expect(parseTabHash('#activity/calls?key=7')).toEqual({ view: 'activity', tab: 'feed', key: '7' })
  expect(parseTabHash('#orgs/settings')).toEqual({ view: 'orgs', tab: 'danger', key: '' })
  expect(parseTabHash('#activity')).toEqual({ view: 'activity', tab: undefined, key: '' })
  // an unknown tab or a key that is not an id is ignored, not trusted
  expect(parseTabHash('#activity/nonsense?key=1;drop')).toEqual({ view: 'activity', tab: undefined, key: '' })
  // every other address is left alone: the catalog's own slashed routes included
  for (const other of ['#platform/seo', '#usage', '#billing', '#catalog', '#start', '']) expect(parseTabHash(other)).toBeNull()
})

test('admins land on Usage, members on Calls; the address names the tab and its key', () => {
  expect(navigation.defaultActTab.call({ canAdmin: true })).toBe('usage')
  expect(navigation.defaultActTab.call({ canAdmin: false })).toBe('feed')
  const at = (state: object) => (view: string) => navigation.tabUrl.call(state, view)
  expect(at({ actTab: 'feed', activityKey: '7', spendFilter: { key: '' } })('activity')).toBe('/app#activity/calls?key=7')
  expect(at({ actTab: 'usage', activityKey: '7', spendFilter: { key: '' } })('activity')).toBe('/app#activity/usage')
  expect(at({ orgTab: 'danger' })('orgs')).toBe('/app#orgs/settings')
  expect(at({})('hub')).toBe('/app#hub')
})
