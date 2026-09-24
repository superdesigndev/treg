import { expect, test } from 'vitest'
import { isJobQuery } from '../src/state/find.js'
import findComputed from '../src/state/findComputed.js'

test('a name filters, a sentence or a question asks', () => {
  expect(isJobQuery('tiktok')).toBe(false)
  expect(isJobQuery('google search console')).toBe(false)
  expect(isJobQuery('why is my blog losing traffic')).toBe(true)
  expect(isJobQuery('who links to me?')).toBe(true)
})

const row = (id: string, capability: string, p: number | null, platform = 'meta-ads') =>
  ({ id, capability, capability_description: capability + ' job', name: id, platform, platform_label: platform, p })

test('rows group by capability, best fit first, providers kept in server order', () => {
  const vm = { find: { verdict: 'strong', rows: [
    row('a.x', 'ads.search', 0.62), row('b.x', 'ads.search', 0.91), row('c.x', 'ads.page', 0.75, 'facebook'),
  ] } }
  const groups = findComputed.findGroups.call(vm)
  expect(groups.map(g => [g.label, g.p])).toEqual([['ads.search job', 0.91], ['ads.page job', 0.75]])
  expect(groups[0].rows.map(r => r.id)).toEqual(['a.x', 'b.x'])
  const strong = findComputed.findStrong.call({ findGroups: groups, find: { high: 0.7 } })
  expect(strong).toHaveLength(2)
})

test('keyword fallback rows keep their order and carry no fit', () => {
  const vm = { find: { verdict: 'keyword', rows: [row('z', 'b', null), row('y', 'a', null)] } }
  const groups = findComputed.findGroups.call(vm)
  expect(groups.map(g => g.label)).toEqual(['b job', 'a job'])
  expect(findComputed.findStrong.call({ findGroups: groups, find: { high: 0.7 } })).toEqual([])
})
