import { expect, test } from 'vitest'
import team from '../src/state/team.js'

function member(orgTab: string, canAdmin = false) {
  const asked: string[] = []
  const ctx: any = {
    ...team, canAdmin, orgTab, activeOrgId: 4, elements: {}, activeSlugNow: 'acme',
    ticket: () => () => true,
    loadApiKeys: async () => [], loadApiKeySpend: () => {},
    api: async (path: string) => { asked.push(path); return [{ email: 'owner@acme.dev', role: 'owner' }] },
  }
  return { ctx, asked }
}

test('a member sent to #billing learns which admins can add funds', async () => {
  const { ctx, asked } = member('billing')
  await ctx.loadOrgAdmin()
  await Promise.resolve()
  expect(ctx.memberFunds).toBe(true)
  expect(ctx.orgTab).toBe('keys')
  expect(asked).toEqual(['/orgs/4/admins'])
  expect(ctx.teamAdmins).toEqual([{ email: 'owner@acme.dev', role: 'owner' }])
})

test('a member on another tab sees no funds note', async () => {
  const { ctx, asked } = member('keys')
  await ctx.loadOrgAdmin()
  expect(ctx.memberFunds).toBe(false)
  expect(asked).toEqual([])
})
