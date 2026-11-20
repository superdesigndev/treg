// The Team page's Showcase tab: what a team's entries say and what an application sends.
// The server (domain/showcase) is the judge of every field; these only shape the form.

export const MAX_APPS = 5
export const MAX_SHOTS = 4

export const CATEGORIES: [string, string][] = [
  ['social-listening', 'Social listening'], ['ads', 'Ads'], ['sales', 'Sales and leads'], ['seo', 'SEO'],
  ['content', 'Content and media'], ['business-ops', 'Business ops'], ['company-data', 'Company data'],
  ['developer-tools', 'Developer tools'], ['other', 'Other'],
]

export type Revision = {
  name: string, url: string, tagline: string, description: string, category: string, maker_name: string,
  x_handle: string, launch_post_url: string, icon_key: string | null, shot_keys: string[],
  show_calls: boolean, show_streak: boolean, show_providers: boolean, show_categories: boolean,
}
export type Preview = Revision & { icon_url: string | null, shot_urls: string[] }
export type TeamKey = { id: number, name: string, prefix: string, last_used_at: string | null, app_id?: number | null }
/** Where an app's activity comes from: all of the team's calls, or only some of its API keys. */
export type Usage = { source: 'team' | 'keys', keys?: TeamKey[] }
export type ShowcaseApp = {
  id: number, org_id: number, status: 'pending' | 'approved' | 'rejected' | 'hidden', version: number,
  usage?: Usage, live: Preview | null, pending: Preview | null, reject_reason: string,
  approved_at: string | null, created_at: string, updated_at: string,
}
export type Picture = { key: string, url: string }
/** The form: the text fields, and pictures kept with their preview so they reorder together. */
export type Draft = Omit<Revision, 'icon_key' | 'shot_keys'> & { icon: Picture | null, shots: Picture[] }

export type Status = { text: string, tone: 'ok' | 'review' | 'bad' | 'off' }

/** The chip for one entry, and a note when a change to a live app was turned down. */
export function statusOf(app: ShowcaseApp): Status {
  if (app.status === 'rejected') return { text: 'Rejected: ' + (app.reject_reason || 'no reason given'), tone: 'bad' }
  if (app.status === 'hidden') return { text: 'Hidden', tone: 'off' }
  if (app.approved_at && app.pending) return { text: 'Live (change in review)', tone: 'review' }
  if (app.approved_at) return { text: 'Live', tone: 'ok' }
  return { text: 'In review', tone: 'review' }
}

/** Entries that count against the team's limit: rejected ones do not. */
export function slotsUsed(apps: ShowcaseApp[]): number {
  return apps.filter(a => a.status !== 'rejected').length
}

export function blankDraft(): Draft {
  return {
    name: '', url: '', tagline: '', description: '', category: '', maker_name: '', x_handle: '', launch_post_url: '',
    icon: null, shots: [], show_calls: false, show_streak: false, show_providers: false, show_categories: false,
  }
}

/** An entry's newest revision (the one in review, else the live one) as a form. */
export function draftOf(app: ShowcaseApp): Draft {
  const rev = app.pending || app.live
  if (!rev) return blankDraft()
  return {
    name: rev.name, url: rev.url, tagline: rev.tagline, description: rev.description, category: rev.category,
    maker_name: rev.maker_name, x_handle: rev.x_handle ? '@' + rev.x_handle : '', launch_post_url: rev.launch_post_url,
    icon: rev.icon_key ? { key: rev.icon_key, url: rev.icon_url || '' } : null,
    shots: rev.shot_keys.map((key, i) => ({ key, url: rev.shot_urls[i] || '' })),
    show_calls: rev.show_calls, show_streak: rev.show_streak, show_providers: rev.show_providers, show_categories: rev.show_categories,
  }
}

/** The JSON the apply and edit routes take. */
export function revisionOf(d: Draft): Revision {
  return {
    name: d.name.trim(), url: d.url.trim(), tagline: d.tagline.trim(), description: d.description.trim(),
    category: d.category, maker_name: d.maker_name.trim(), x_handle: d.x_handle.trim(),
    launch_post_url: d.launch_post_url.trim(), icon_key: d.icon ? d.icon.key : null,
    shot_keys: d.shots.map(s => s.key),
    show_calls: d.show_calls, show_streak: d.show_streak, show_providers: d.show_providers, show_categories: d.show_categories,
  }
}

/** A copy of `list` with item `i` moved by `by` places; out of range leaves it as it is. */
export function moved<T>(list: T[], i: number, by: number): T[] {
  const j = i + by
  if (i < 0 || i >= list.length || j < 0 || j >= list.length) return list.slice()
  const out = list.slice()
  ;[out[i], out[j]] = [out[j]!, out[i]!]
  return out
}

/** One line for the list: where the app's activity comes from. */
export function usageText(usage: Usage | undefined): string {
  if (!usage || usage.source === 'team') return 'Activity: the whole team'
  const names = (usage.keys || []).map(k => k.name)
  return names.length ? 'Activity: API keys ' + names.join(', ') : 'Activity: API keys'
}

/** The usage form value for an entry: the source, and the linked key ids. */
export function usageDraftOf(app: ShowcaseApp | null): { source: 'team' | 'keys', keyIds: number[] } {
  const u = app && app.usage
  return { source: u && u.source === 'keys' ? 'keys' : 'team', keyIds: ((u && u.keys) || []).map(k => k.id) }
}

/** Whether the usage form differs from what the entry has now. */
export function usageChanged(app: ShowcaseApp | null, d: { source: string, keyIds: number[] }): boolean {
  const now = usageDraftOf(app)
  if (d.source !== now.source) return true
  return d.source === 'keys' && [...d.keyIds].sort().join(',') !== [...now.keyIds].sort().join(',')
}

export const SHOW_FLAGS = ['show_calls', 'show_streak', 'show_providers', 'show_categories'] as const

/** Whether two revisions differ in a reviewed field (anything but the show flags, which apply at once). */
export function reviewedChanged(a: Revision, b: Revision): boolean {
  const strip = (r: Revision) => JSON.stringify({ ...r, show_calls: 0, show_streak: 0, show_providers: 0, show_categories: 0 })
  return strip(a) !== strip(b)
}
