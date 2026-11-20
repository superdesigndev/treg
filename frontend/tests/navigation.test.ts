import { expect, test } from 'vitest'
import navigation, { parseTabHash } from '../src/state/navigation.js'

test('only Activity and Team addresses carry a tab, and Activity a key id', () => {
  expect(parseTabHash('#activity/usage')).toEqual({ view: 'activity', tab: 'usage', key: '' })
  expect(parseTabHash('#activity/calls?key=7')).toEqual({ view: 'activity', tab: 'feed', key: '7' })
  expect(parseTabHash('#activity/usage?key=12')).toEqual({ view: 'activity', tab: 'usage', key: '12' })
  expect(parseTabHash('#orgs/settings')).toEqual({ view: 'orgs', tab: 'danger', key: '' })
  expect(parseTabHash('#orgs/showcase')).toEqual({ view: 'orgs', tab: 'showcase', key: '' })
  expect(parseTabHash('#orgs/sso')).toEqual({ view: 'orgs', tab: 'sso', key: '' })
  expect(parseTabHash('#activity')).toEqual({ view: 'activity', tab: undefined, key: '' })
  // an unknown tab or a key that is not an id is ignored, not trusted
  expect(parseTabHash('#activity/nonsense?key=1;drop')).toEqual({ view: 'activity', tab: undefined, key: '' })
  // every other address is left alone: the catalog's own slashed routes included
  for (const other of ['#platform/seo', '#usage', '#billing', '#catalog', '#start', '']) expect(parseTabHash(other)).toBeNull()
})

test('the address names the tab, and on Calls its key; the spend chart\'s key stays out of it', () => {
  const at = (state: object) => (view: string) => navigation.tabUrl.call(state, view)
  expect(at({ actTab: 'feed', activityKey: '7', spendFilter: { key: '' } })('activity')).toBe('/app#activity/calls?key=7')
  expect(at({ actTab: 'usage', activityKey: '7', spendFilter: { key: '' } })('activity')).toBe('/app#activity/usage')
  expect(at({ actTab: 'usage', activityKey: '', spendFilter: { key: '9' } })('activity')).toBe('/app#activity/usage')
  expect(at({ orgTab: 'danger' })('orgs')).toBe('/app#orgs/settings')
  expect(at({ orgTab: 'showcase' })('orgs')).toBe('/app#orgs/showcase')
  expect(at({ orgTab: 'sso' })('orgs')).toBe('/app#orgs/sso')
  expect(at({})('hub')).toBe('/app#hub')
})
