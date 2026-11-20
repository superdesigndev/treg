import { expect, test, vi } from 'vitest'
import { blankDraft, draftOf, moved, revisionOf, slotsUsed, statusOf, type ShowcaseApp } from '../src/pages/team/showcase'
import { imageProblem, initialsOf, uploadImage, UploadError } from '../src/images'

const rev = {
  name: 'Lead scout', url: 'https://scout.example', tagline: 'Finds leads', description: '', category: 'sales',
  maker_name: 'Ada', x_handle: 'ada', launch_post_url: '', icon_key: 'showcase_icon/a.png', shot_keys: ['s1', 's2'],
  show_calls: true, show_streak: false, show_providers: false, show_categories: true,
  icon_url: '/images/staging/showcase_icon/a.png', shot_urls: ['/u1', '/u2'],
}
const app = (over: Partial<ShowcaseApp>): ShowcaseApp => ({
  id: 1, org_id: 2, status: 'pending', version: 1, live: null, pending: rev, reject_reason: '',
  approved_at: null, created_at: '', updated_at: '', ...over,
})

test('an entry says where it is in review', () => {
  expect(statusOf(app({})).text).toBe('In review')
  expect(statusOf(app({ status: 'approved', approved_at: 'x', live: rev, pending: null })).text).toBe('Live')
  expect(statusOf(app({ status: 'approved', approved_at: 'x', live: rev })).text).toBe('Live (change in review)')
  expect(statusOf(app({ status: 'rejected', reject_reason: 'no link' })).text).toBe('Rejected: no link')
  expect(statusOf(app({ status: 'hidden', approved_at: 'x', live: rev, pending: null })).text).toBe('Hidden')
})

test('rejected entries do not count against the limit', () => {
  expect(slotsUsed([app({}), app({ status: 'rejected' }), app({ status: 'hidden' })])).toBe(2)
})

test('the form round-trips an entry into the JSON the server takes', () => {
  const d = draftOf(app({}))
  expect(d.x_handle).toBe('@ada')
  expect(d.shots).toEqual([{ key: 's1', url: '/u1' }, { key: 's2', url: '/u2' }])
  const out = revisionOf({ ...d, name: '  Lead scout ', shots: moved(d.shots, 1, -1) })
  expect(out).toMatchObject({ name: 'Lead scout', x_handle: '@ada', icon_key: 'showcase_icon/a.png', shot_keys: ['s2', 's1'] })
  expect(Object.keys(out).sort()).toEqual(Object.keys(rev).filter(k => k !== 'icon_url' && k !== 'shot_urls').sort())
  expect(revisionOf(blankDraft()).icon_key).toBeNull()
})

test('moving past either end leaves the order as it is', () => {
  expect(moved([1, 2, 3], 0, -1)).toEqual([1, 2, 3])
  expect(moved([1, 2, 3], 2, 1)).toEqual([1, 2, 3])
  expect(moved([1, 2, 3], 0, 1)).toEqual([2, 1, 3])
})

test('a picture that cannot pass is stopped before upload', () => {
  expect(imageProblem('showcase_icon', { type: 'image/gif', size: 10 })).toMatch(/PNG, JPEG or WebP/)
  expect(imageProblem('showcase_icon', { type: 'image/png', size: 3_000_000 })).toMatch(/too large/)
  expect(imageProblem('showcase_shot', { type: 'image/webp', size: 3_000_000 })).toBe('')
})

test('an upload sends the raw file with its own type and reports the server\'s words', async () => {
  const file = new Blob(['x'], { type: 'image/png' })
  const ok = vi.fn(async () => new Response(JSON.stringify({ key: 'k', kind: 'team_avatar', preview_url: '/p' }), { status: 200 }))
  expect(await uploadImage('team_avatar', file, { 'X-Treg-Org': 't' }, ok as typeof fetch)).toEqual({ key: 'k', kind: 'team_avatar', preview_url: '/p' })
  const [url, init] = ok.mock.calls[0] as unknown as [string, RequestInit]
  expect(url).toBe('/images/team_avatar')
  expect(init.body).toBe(file)
  expect(init.headers).toEqual({ 'X-Treg-Org': 't', 'content-type': 'image/png' })
  const off = vi.fn(async () => new Response(JSON.stringify({ detail: 'picture uploads are not enabled on this registry' }), { status: 503 }))
  const err = await uploadImage('team_avatar', file, {}, off as typeof fetch).catch(e => e)
  expect(err).toBeInstanceOf(UploadError)
  expect([err.status, err.detail]).toEqual([503, 'picture uploads are not enabled on this registry'])
})

test('initials come from a name or an email', () => {
  expect(initialsOf('ada.lovelace@example.com')).toBe('AL')
  expect(initialsOf('Superdesign')).toBe('SU')
  expect(initialsOf('Lead Scout')).toBe('LS')
  expect(initialsOf('')).toBe('?')
})

test('usage reads as one line and changes only when the source or keys differ', async () => {
  const { usageText, usageDraftOf, usageChanged } = await import('../src/pages/team/showcase')
  const keyed = app({ usage: { source: 'keys', keys: [{ id: 7, name: 'prod', prefix: 'treg_ab', last_used_at: null }] } })
  expect(usageText(undefined)).toBe('Activity: the whole team')
  expect(usageText(keyed.usage)).toBe('Activity: API keys prod')
  expect(usageDraftOf(keyed)).toEqual({ source: 'keys', keyIds: [7] })
  expect(usageChanged(keyed, { source: 'keys', keyIds: [7] })).toBe(false)
  expect(usageChanged(keyed, { source: 'keys', keyIds: [7, 8] })).toBe(true)
  expect(usageChanged(keyed, { source: 'team', keyIds: [] })).toBe(true)
  expect(usageChanged(null, { source: 'team', keyIds: [] })).toBe(false)
  expect(usageChanged(null, { source: 'keys', keyIds: [3] })).toBe(true)
})

test('only a change outside the show flags goes to review', async () => {
  const { reviewedChanged, revisionOf, draftOf } = await import('../src/pages/team/showcase')
  const base = revisionOf(draftOf(app({})))
  expect(reviewedChanged(base, { ...base, show_calls: !base.show_calls })).toBe(false)
  expect(reviewedChanged(base, { ...base, tagline: 'New line' })).toBe(true)
})
