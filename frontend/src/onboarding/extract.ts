// Each first task shows its answer the way that answer is read: a person as a contact card, places
// with stars, videos as covers. These turn the raw call answers (routed
// endpoints wrap rows in `output`; the provider's own row shapes differ) into those views. Pure
// functions: tests/onboarding.test.ts runs them on real answers.

type Obj = Record<string, any>

export type View =
  | { view: 'person'; person: { name: string; title: string; company: string; email: string; verified: boolean } }
  | { view: 'people'; rows: { name: string; title: string; tag: string }[] }
  | { view: 'company'; company: { name: string; domain: string; facts: [string, string][] } }
  | { view: 'keywords'; rows: { kw: string; vol: number; kd: number | null; cpc: number | null }[] }
  | { view: 'serp'; rows: { pos: number; title: string; site: string; you: boolean }[]; missing: string }
  | { view: 'maps'; rows: { name: string; rating: number | null; reviews: number; category: string; website: string }[] }
  | { view: 'social'; rows: { platform: 'X' | 'Reddit'; author: string; date: string; text: string; likes: string }[] }
  | { view: 'videos'; rows: { desc: string; author: string; views: number; img: string }[] }
  | { view: 'scrape'; url: string; cols: string[]; rows: string[][] }

export type Extracted = View | null

const isObj = (v: unknown): v is Obj => !!v && typeof v === 'object' && !Array.isArray(v)

// The first non-empty value among dotted paths.
export function pick(o: unknown, ...paths: string[]): any {
  for (const p of paths) {
    let cur: any = o
    for (const k of p.split('.')) cur = isObj(cur) ? cur[k] : undefined
    if (cur !== undefined && cur !== null && cur !== '') return cur
  }
  return undefined
}

const out = (b: unknown): Obj => (isObj(b) && isObj(b.output) ? b.output : isObj(b) ? b : {})
const list = (v: unknown): Obj[] => (Array.isArray(v) ? v.filter(isObj) : [])
const str = (v: unknown) => (v === undefined || v === null ? '' : String(v))
const num = (v: unknown) => (typeof v === 'number' ? v : Number.isFinite(Number(v)) && v !== '' && v !== null ? Number(v) : NaN)
const host = (u: string) => { try { return new URL(u.includes('://') ? u : 'https://' + u).hostname.replace(/^www\./, '') } catch { return '' } }
const titleCase = (s: string) => s.replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())

export const compact = (n: number) =>
  n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M' : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'K' : String(n)

const count = (n: number, word: string) => compact(n) + ' ' + word + (n === 1 ? '' : 's')

// ------------------------------------------------------------------------------- the ten views
function person(bodies: unknown[], input: string): Extracted {
  const o = out(bodies[0])
  const email = str(o.email)
  if (!email) return null
  const raw = pick(bodies[0], 'raw.data', 'raw') || {}
  const name = [o.first_name, o.last_name].filter(Boolean).join(' ') || input.split(/\s+at\s+/)[0]
  const verified = o.verified === true
  const company = str(pick(raw, 'company_name')) || host(email.split('@')[1] || '')
  return { view: 'person', person: { name, title: str(pick(raw, 'title', 'job_title', 'position')), company, email, verified } }
}

function people(bodies: unknown[]): Extracted {
  const o = out(bodies[0])
  const rows = list(o.people).map((p) => ({
    name: str(pick(p, 'full_name', 'name')) || [p.first_name, p.last_name].filter(Boolean).join(' '),
    title: str(pick(p, 'title', 'job_title', 'headline', 'position')),
    tag: str(pick(p, 'locality', 'location', 'city')).replace(/, United States$/, ''),
  })).filter((r) => r.name)
  return rows.length ? { view: 'people', rows } : null
}

const COMPANY_FIELDS: [string, string][] = [
  ['description', 'About'], ['industry', 'Industry'], ['founded', 'Founded'], ['location', 'Based in'], ['employees', 'Employees'],
]

function company(bodies: unknown[], input: string): Extracted {
  const o = out(bodies[0])
  if (!o.name) return null
  const facts: [string, string][] = []
  for (const [k, label] of COMPANY_FIELDS) {
    const v = str(o[k])
    if (v) facts.push([label, k === 'industry' ? titleCase(v) : v])
  }
  const tagline = str(pick(bodies[0], 'raw.descriptions.tagline'))
  if (tagline) facts.unshift(['Tagline', tagline])
  const domain = str(o.domain) || host(str(o.website)) || input
  return { view: 'company', company: { name: str(o.name), domain, facts } }
}

function keywords(bodies: unknown[]): Extracted {
  const rows = list(out(bodies[0]).keywords).map((k) => {
    const d = isObj(k.keyword_data) ? k.keyword_data : k
    return {
      kw: str(pick(d, 'keyword', 'text', 'phrase')),
      vol: num(pick(d, 'keyword_info.search_volume', 'search_volume', 'volume', 'avg_monthly_searches')),
      kd: num(pick(d, 'keyword_properties.keyword_difficulty', 'keyword_difficulty', 'difficulty')),
      cpc: num(pick(d, 'keyword_info.cpc', 'cpc')),
    }
  }).filter((r) => r.kw && Number.isFinite(r.vol))
    .map((r) => ({ ...r, kd: Number.isFinite(r.kd) ? r.kd : null, cpc: Number.isFinite(r.cpc) ? r.cpc : null }))
    .sort((a, b) => b.vol - a.vol)
  return rows.length ? { view: 'keywords', rows } : null
}

function serp(bodies: unknown[], _input: string, own: string): Extracted {
  const rows = list(out(bodies[0]).results).map((r, i) => {
    const link = str(pick(r, 'url', 'link', 'href'))
    return { pos: num(pick(r, 'position', 'rank')) || i + 1, title: str(pick(r, 'title')), site: host(link), you: false }
  }).filter((r) => r.site).slice(0, 10)
  if (!rows.length) return null
  const ownHost = own ? host(own) : ''
  for (const r of rows) r.you = !!ownHost && (r.site === ownHost || r.site.endsWith('.' + ownHost))
  return { view: 'serp', rows, missing: ownHost && !rows.some((r) => r.you) ? ownHost : '' }
}

function maps(bodies: unknown[]): Extracted {
  const rows = list(out(bodies[0]).places).map((p) => {
    const cats = pick(p, 'categories', 'category', 'type')
    return {
      name: str(pick(p, 'name', 'title')),
      rating: Number.isFinite(num(pick(p, 'rating', 'stars'))) ? num(pick(p, 'rating', 'stars')) : null,
      reviews: num(pick(p, 'review_count', 'reviews_count', 'reviewsCount', 'user_ratings_total')) || 0,
      category: titleCase(str(Array.isArray(cats) ? cats[0] : cats)),
      website: host(str(pick(p, 'website', 'site', 'url'))),
    }
  }).filter((r) => r.name)
  return rows.length ? { view: 'maps', rows } : null
}

const ago = (sec: number) => {
  if (!sec) return ''
  const d = Math.max(0, Date.now() / 1000 - sec)
  return d < 3600 ? Math.round(d / 60) + 'm' : d < 86400 ? Math.round(d / 3600) + 'h' : Math.round(d / 86400) + 'd'
}

function social(bodies: unknown[]): Extracted {
  const x = list(out(bodies[0]).posts).map((p) => ({
    platform: 'X' as const, author: '@' + str(pick(p, 'authorUsername', 'author.username', 'user.screen_name', 'username')),
    date: ago(num(pick(p, 'createdUtc', 'created_at_utc')) || Date.parse(str(pick(p, 'createdAt', 'created_at'))) / 1000 || 0),
    text: str(pick(p, 'text', 'full_text', 'content')),
    likes: count(num(pick(p, 'likeCount', 'favorite_count', 'likes')) || 0, 'like'), n: num(pick(p, 'likeCount', 'favorite_count', 'likes')) || 0,
  })).filter((p) => p.text)
  const rd = list(pick(bodies[1], 'posts', 'output.posts', 'data')).map((p) => ({
    platform: 'Reddit' as const, author: 'r/' + str(pick(p, 'subreddit')),
    date: ago(num(pick(p, 'created_utc', 'created')) || 0),
    text: str(pick(p, 'title')), likes: count(num(pick(p, 'ups', 'score')) || 0, 'upvote') + ' · ' + count(num(p.num_comments) || 0, 'comment'),
    n: num(pick(p, 'ups', 'score')) || 0,
  })).filter((p) => p.text)
  // The loudest post leads; then X and Reddit alternate, so the first rows show both before the
  // rest fold away.
  const xs = x.sort((a, b) => b.n - a.n).slice(0, 6), rs = rd.slice(0, 6)
  const top = [...xs, ...rs].sort((a, b) => b.n - a.n)[0]
  if (!top) return null
  const rest = Array.from({ length: Math.max(xs.length, rs.length) }, (_, i) => [xs[i], rs[i]]).flat()
    .filter((r) => r && r !== top) as typeof xs
  return { view: 'social', rows: [top, ...rest].map(({ n: _n, ...r }) => r) }
}

function videos(bodies: unknown[]): Extracted {
  const rows = list(out(bodies[0]).videos).map((v) => {
    const it = isObj(v.aweme_info) ? v.aweme_info : v
    const cover = pick(it, 'video.cover.url_list', 'video.origin_cover.url_list', 'cover', 'thumbnail')
    return {
      desc: str(pick(it, 'desc', 'title', 'description')).replace(/\s+/g, ' '),
      author: '@' + str(pick(it, 'author.unique_id', 'author.uniqueId', 'author_name')),
      views: num(pick(it, 'statistics.play_count', 'stats.playCount', 'play_count', 'views')) || 0,
      img: str(Array.isArray(cover) ? cover[0] : cover),
    }
  }).filter((r) => r.desc || r.img).sort((a, b) => b.views - a.views).slice(0, 12)
  return rows.length ? { view: 'videos', rows } : null
}

// A page becomes rows: its first Markdown table when it has one, else one row per section
// (heading, the first figure under it, the first line of detail).
export function pageTable(md: string): { cols: string[]; rows: string[][] } {
  const lines = md.split('\n')
  const t = lines.findIndex((l, i) => /^\s*\|.*\|\s*$/.test(l) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || ''))
  if (t >= 0) {
    const cells = (l: string) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim().replace(/\*\*/g, ''))
    const rows: string[][] = []
    for (let i = t + 2; i < lines.length && /^\s*\|/.test(lines[i]); i++) rows.push(cells(lines[i]))
    return { cols: cells(lines[t]), rows: rows.slice(0, 12) }
  }
  const rows: string[][] = []
  let cur: string[] | null = null
  for (const raw of lines) {
    const l = raw.trim()
    const h = /^#{2,4}\s+(.+)$/.exec(l)
    if (h) { cur = [h[1].replace(/[*_`]/g, ''), '', '']; rows.push(cur); continue }
    if (!cur || !l) continue
    const text = l.replace(/^[*-]\s+/, '').replace(/[*_`]/g, '').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    if (!cur[1] && /[$€£¥]\s?\d|\d+(\.\d+)?\s?(%|\/mo|per)/i.test(text) && text.length < 40) cur[1] = text
    else if (!cur[2] && text.length > 2) cur[2] = text.slice(0, 120)
  }
  return { cols: ['Section', 'Figure', 'Detail'], rows: rows.filter((r) => r[1] || r[2]).slice(0, 12) }
}

function scrape(bodies: unknown[], input: string): Extracted {
  const page = list(out(bodies[0]).pages)[0]
  if (!page) return null
  const md = str(pick(page, 'markdown', 'text', 'content'))
  const { cols, rows } = pageTable(md)
  if (!rows.length) return null
  const url = str(pick(page, 'final_url', 'url')) || input
  return { view: 'scrape', url, cols, rows }
}

const VIEWS: Record<string, (bodies: unknown[], input: string, own: string) => Extracted> = {
  person, people, company, keywords, serp, maps, social, videos, scrape,
}

export function extract(view: string, bodies: unknown[], input: string, own = ''): Extracted {
  try {
    return VIEWS[view]?.(bodies, input, own) ?? null
  } catch {
    return null
  }
}
