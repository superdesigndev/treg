// The rules of TikTok's Content Sharing Guidelines for a direct post, kept apart from the Try
// drawer's TikTok form (dialogs/TikTokPostForm.vue) so they can be tested. The browser sends the
// video straight to TikTok's upload URL; treg relays only the init, creator and status calls.

// The one tool whose Try drawer is TikTok's post screen rather than a test console.
export const TIKTOK_PUBLISH = 'tiktok.tiktok.video.publish.init'

export type Creator = {
  ok: boolean
  error?: { code: string, message: string } | null
  creator_nickname?: string
  creator_username?: string
  creator_avatar_url?: string
  privacy_level_options?: string[]
  comment_disabled?: boolean
  duet_disabled?: boolean
  stitch_disabled?: boolean
  max_video_post_duration_sec?: number
}

export type Form = {
  title: string
  privacy_level: string
  allow_comment: boolean
  allow_duet: boolean
  allow_stitch: boolean
  disclose: boolean
  your_brand: boolean
  branded_content: boolean
  is_aigc: boolean
}

export const PRIVACY_WORDS: Record<string, string> = {
  PUBLIC_TO_EVERYONE: 'Everyone',
  MUTUAL_FOLLOW_FRIENDS: 'Friends',
  FOLLOWER_OF_CREATOR: 'Followers',
  SELF_ONLY: 'Only me',
}

export const BRANDED_PRIVATE = 'Branded content visibility cannot be set to private.'
export const PICK_ONE = 'You need to indicate if your content promotes yourself, a third party, or both.'

// Nothing is chosen for the person: no privacy level and no interaction is preset.
export function emptyForm(title: string): Form {
  return { title, privacy_level: '', allow_comment: false, allow_duet: false, allow_stitch: false,
    disclose: false, your_brand: false, branded_content: false, is_aigc: false }
}

export function privacyOptions(creator: Creator, form: Form) {
  return (creator.privacy_level_options || []).map(value => ({
    value,
    label: PRIVACY_WORDS[value] || value,
    disabled: value === 'SELF_ONLY' && form.disclose && form.branded_content,
  }))
}

// "Branded content" is not allowed with "Only me": the box is off while private is chosen.
export function brandedDisabled(form: Form) {
  return form.privacy_level === 'SELF_ONLY'
}

export function labelPrompt(form: Form) {
  if (!form.disclose) return ''
  if (form.branded_content) return "Your video will be labeled as 'Paid partnership'"
  if (form.your_brand) return "Your video will be labeled as 'Promotional content'"
  return ''
}

// The consent line names TikTok's own pages; the form links each name to it.
export const MUSIC_URL = 'https://www.tiktok.com/legal/page/global/music-usage-confirmation/en'
export const BRANDED_URL = 'https://www.tiktok.com/legal/page/global/bc-policy/en'

// The consent line, as text parts; a part with an href is a link to that TikTok page.
export function declaration(form: Form): { text: string, href?: string }[] {
  const music = { text: 'Music Usage Confirmation', href: MUSIC_URL }
  return form.disclose && form.branded_content
    ? [{ text: "By posting, you agree to TikTok's " }, { text: 'Branded Content Policy', href: BRANDED_URL }, { text: ' and ' }, music]
    : [{ text: "By posting, you agree to TikTok's " }, music]
}

export function tooLong(creator: Creator, seconds: number | null) {
  const max = creator.max_video_post_duration_sec
  return !!(max && seconds && seconds > max)
}

// Why the Post button is off, or '' when the person may post.
export function blocker(creator: Creator | null, form: Form, seconds: number | null) {
  if (!creator) return 'Loading your TikTok account…'
  if (!creator.ok) return 'TikTok says this account cannot post right now. Try again later.'
  if (tooLong(creator, seconds)) return `This video is longer than the ${creator.max_video_post_duration_sec} seconds TikTok allows for this account.`
  if (!form.privacy_level) return 'Choose who can see this video.'
  if (form.disclose && !form.your_brand && !form.branded_content) return PICK_ONE
  if (form.disclose && form.branded_content && form.privacy_level === 'SELF_ONLY') return BRANDED_PRIVATE
  return ''
}

// The interaction boxes TikTok shows: a box the creator turned off in TikTok is greyed out.
export function interactions(creator: Creator) {
  return [
    { key: 'allow_comment' as const, label: 'Allow comment', disabled: !!creator.comment_disabled },
    { key: 'allow_duet' as const, label: 'Allow Duet', disabled: !!creator.duet_disabled },
    { key: 'allow_stitch' as const, label: 'Allow Stitch', disabled: !!creator.stitch_disabled },
  ]
}

// TikTok's own limits for a FILE_UPLOAD video (Content Posting API, media transfer guide).
export const MAX_BYTES = 4 * 1024 ** 3
export const VIDEO_TYPES = ['video/mp4', 'video/quicktime', 'video/webm']
const MB = 1024 * 1024

export function fileProblem(file: { size: number, type: string }) {
  if (!VIDEO_TYPES.includes(file.type)) return 'TikTok takes MP4, MOV or WebM videos.'
  if (file.size > MAX_BYTES) return 'TikTok takes videos up to 4 GB.'
  if (!file.size) return 'This file is empty.'
  return ''
}

// A chunk is 5 MB to 64 MB: a file up to 64 MB goes whole, a larger one in 10 MB chunks with
// the remainder in the last chunk (catalog note on tiktok.tiktok.video.publish.init).
export function chunkPlan(size: number) {
  const chunk = size <= 64 * MB ? size : 10 * MB
  const count = Math.max(1, Math.floor(size / chunk))
  const ranges = Array.from({ length: count }, (_, i) => ({
    start: i * chunk, end: i === count - 1 ? size - 1 : (i + 1) * chunk - 1 }))
  return { source_info: { source: 'FILE_UPLOAD', video_size: size, chunk_size: chunk, total_chunk_count: count }, ranges }
}

// The form as TikTok's post_info: an unticked "Allow" box is a disable_* flag.
export function postInfo(form: Form) {
  return {
    title: form.title,
    privacy_level: form.privacy_level,
    disable_comment: !form.allow_comment,
    disable_duet: !form.allow_duet,
    disable_stitch: !form.allow_stitch,
    brand_content_toggle: form.disclose && form.branded_content,
    brand_organic_toggle: form.disclose && form.your_brand,
    is_aigc: form.is_aigc,
  }
}

// The bytes go only to TikTok's own upload host, never wherever an answer says.
export function uploadUrlOk(url: string) {
  try {
    const u = new URL(url)
    return u.protocol === 'https:' && u.hostname.endsWith('.tiktokapis.com')
  } catch { return false }
}

// TikTok's error codes a person can act on, in their words.
const ERROR_WORDS: Record<string, string> = {
  unaudited_client_can_only_post_to_private_accounts:
    'TikTok has not approved treg for public posts yet. Set your TikTok account to private and choose "Only me".',
  spam_risk_too_many_posts: 'This account has posted too often today. TikTok asks you to wait.',
  spam_risk_user_banned_from_posting: 'TikTok does not let this account post right now.',
  privacy_level_option_mismatch: 'TikTok does not allow this privacy option for this account.',
  scope_not_authorized: 'This connection cannot publish. Reconnect TikTok with "Read and publish".',
  access_token_invalid: 'The TikTok connection expired. Reconnect TikTok.',
  rate_limit_exceeded: 'TikTok is busy. Try again in a minute.',
}

export function tiktokWords(error: { code?: string, message?: string } | null | undefined) {
  if (!error) return 'TikTok did not answer.'
  return (error.code && ERROR_WORDS[error.code]) || error.message || error.code || 'TikTok refused the request.'
}
