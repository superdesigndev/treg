import { expect, test } from 'vitest'
import {
  blocker, brandedDisabled, BRANDED_PRIVATE, BRANDED_URL, chunkPlan, declaration, emptyForm, fileProblem,
  interactions, labelPrompt, MUSIC_URL, PICK_ONE, postInfo, privacyOptions, tiktokWords, uploadUrlOk,
  type Creator,
} from '../src/tiktokPost'

const said = (parts: { text: string }[]) => parts.map(p => p.text).join('')

const creator: Creator = {
  ok: true, creator_nickname: 'Taus', privacy_level_options: ['PUBLIC_TO_EVERYONE', 'SELF_ONLY'],
  comment_disabled: false, duet_disabled: true, stitch_disabled: false, max_video_post_duration_sec: 60,
}

test('nothing is chosen for the person', () => {
  const f = emptyForm('caption')
  expect(f).toMatchObject({ title: 'caption', privacy_level: '', allow_comment: false, allow_duet: false,
    allow_stitch: false, disclose: false, is_aigc: false })
  expect(blocker(creator, f, 10)).toBe('Choose who can see this video.')
})

test('privacy options are only the ones TikTok returned', () => {
  expect(privacyOptions(creator, emptyForm('')).map(o => o.label)).toEqual(['Everyone', 'Only me'])
})

test('an interaction the creator turned off is greyed out', () => {
  expect(interactions(creator).find(b => b.key === 'allow_duet')!.disabled).toBe(true)
  expect(interactions(creator).find(b => b.key === 'allow_comment')!.disabled).toBe(false)
})

test('a video longer than the creator may post blocks the button', () => {
  const f = { ...emptyForm(''), privacy_level: 'PUBLIC_TO_EVERYONE' }
  expect(blocker(creator, f, 61)).toContain('60 seconds')
  expect(blocker(creator, f, 60)).toBe('')
})

test('a creator who cannot post blocks the button', () => {
  expect(blocker({ ok: false }, emptyForm(''), null)).toContain('cannot post')
})

test('commercial disclosure needs one box and changes the label and declaration', () => {
  const f = { ...emptyForm(''), privacy_level: 'PUBLIC_TO_EVERYONE', disclose: true }
  expect(blocker(creator, f, 1)).toBe(PICK_ONE)
  expect(said(declaration(f))).toBe("By posting, you agree to TikTok's Music Usage Confirmation")
  f.your_brand = true
  expect(labelPrompt(f)).toBe("Your video will be labeled as 'Promotional content'")
  expect(said(declaration(f))).toBe("By posting, you agree to TikTok's Music Usage Confirmation")
  f.branded_content = true
  expect(labelPrompt(f)).toBe("Your video will be labeled as 'Paid partnership'")
  expect(said(declaration(f))).toBe("By posting, you agree to TikTok's Branded Content Policy and Music Usage Confirmation")
  expect(declaration(f).filter(p => p.href).map(p => p.href)).toEqual([BRANDED_URL, MUSIC_URL])
  expect(blocker(creator, f, 1)).toBe('')
})

test('branded content cannot be private, from either side', () => {
  const priv = { ...emptyForm(''), privacy_level: 'SELF_ONLY', disclose: true }
  expect(brandedDisabled(priv)).toBe(true)
  const branded = { ...emptyForm(''), disclose: true, branded_content: true }
  expect(privacyOptions(creator, branded).find(o => o.value === 'SELF_ONLY')!.disabled).toBe(true)
  expect(blocker(creator, { ...branded, privacy_level: 'SELF_ONLY' }, 1)).toBe(BRANDED_PRIVATE)
})


const MB = 1024 * 1024

test('a video up to 64 MB goes as one chunk', () => {
  const p = chunkPlan(64 * MB)
  expect(p.source_info).toEqual({ source: 'FILE_UPLOAD', video_size: 64 * MB, chunk_size: 64 * MB, total_chunk_count: 1 })
  expect(p.ranges).toEqual([{ start: 0, end: 64 * MB - 1 }])
})

test('a larger video goes in 10 MB chunks, the last one carries the rest', () => {
  const size = 65 * MB + 123
  const p = chunkPlan(size)
  expect(p.source_info.chunk_size).toBe(10 * MB)
  expect(p.source_info.total_chunk_count).toBe(6)
  expect(p.ranges[5]).toEqual({ start: 50 * MB, end: size - 1 })
  expect(p.ranges[0]).toEqual({ start: 0, end: 10 * MB - 1 })
})

test('an unticked box tells TikTok to turn that interaction off', () => {
  const f = { ...emptyForm('hi'), privacy_level: 'SELF_ONLY', allow_comment: true, disclose: true, your_brand: true }
  expect(postInfo(f)).toEqual({ title: 'hi', privacy_level: 'SELF_ONLY', disable_comment: false,
    disable_duet: true, disable_stitch: true, brand_content_toggle: false, brand_organic_toggle: true, is_aigc: false })
})

test('the bytes go only to an https TikTok upload host', () => {
  expect(uploadUrlOk('https://open-upload-sg.tiktokapis.com/upload?upload_id=1')).toBe(true)
  expect(uploadUrlOk('http://open-upload.tiktokapis.com/upload')).toBe(false)
  expect(uploadUrlOk('https://tiktokapis.com.evil.example/upload')).toBe(false)
  expect(uploadUrlOk('not a url')).toBe(false)
})

test('only a TikTok video type and size passes', () => {
  expect(fileProblem({ size: 10, type: 'video/mp4' })).toBe('')
  expect(fileProblem({ size: 10, type: 'image/png' })).toContain('MP4')
  expect(fileProblem({ size: 5 * 1024 ** 3, type: 'video/mp4' })).toContain('4 GB')
})

test('the unaudited-app refusal is said in plain words', () => {
  expect(tiktokWords({ code: 'unaudited_client_can_only_post_to_private_accounts' })).toContain('private')
  expect(tiktokWords({ code: 'something_new', message: 'raw message' })).toBe('raw message')
})
