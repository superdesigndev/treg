import { expect, test, vi } from 'vitest'

vi.stubGlobal('location', { origin: 'https://treg.test' })  // data.ts reads it at import
const { listedOauthGroups, oauthGroups } = await import('../src/agent-setup/data')

const services = (groups: { items: { s: string }[] }[]) => groups.flatMap(g => g.items.map(p => p.s))

test('a provider left out of /oauth/providers loses its chip; no listing yet shows every chip', () => {
  const listing = services(oauthGroups).filter(s => s !== 'google-business-profile').map(service => ({ service }))
  const shown = services(listedOauthGroups(listing))
  expect(shown).not.toContain('google-business-profile')
  expect(shown).toContain('google-search-console')
  expect(listedOauthGroups([])).toBe(oauthGroups)
  expect(listedOauthGroups(undefined)).toBe(oauthGroups)
})

test('a group whose every provider is left out goes with them', () => {
  const groups = listedOauthGroups([{ service: 'x' }])
  expect(groups.map(g => g.label)).toEqual(['Post on social'])
})
