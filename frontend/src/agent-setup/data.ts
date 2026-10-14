// The onboarding setup data shared by the Dashboard welcome flow and Enrich Arena. This directory
// is the one source: the Dashboard imports it, and the build also compiles it to the classic
// `/agent-setup.js` script for the standalone Arena page (see index.ts). No credentials are stored.

export type Agent = { id: string, name: string, icon: string | null, plugin?: string, connector?: boolean }
export type Example = { k: string, cat: string, logo: string, avatar?: string, show?: string, prompt: string }
export type OAuthGroup = { label: string, items: { s: string, n: string }[], soon: { s: string, n: string }[] }
export type IconUrl = (icon: string | null | undefined, theme?: string) => string

// Ordered by how many people who pick each agent go on to make a call; the first is the default.
// 'other' stays last, as the catch-all.
export const agents: Agent[] = [
  {id:'claude-code',name:'Claude Code',icon:'claudecode-color'},
  {id:'codex',name:'Codex',icon:'codex-color'},
  {id:'claudeai',name:'Claude.ai',icon:'claude-color',connector:true},
  {id:'grokbot',name:'Grok Bot',icon:'/logos/agents/grokbot.png',plugin:'https://x.ai/bot/plugin/55647425'},
  {id:'hermes',name:'Hermes Agent',icon:'hermesagent'},
  {id:'cursor',name:'Cursor',icon:'cursor'},
]
export const moreAgents: Agent[] = [
  {id:'openclaw',name:'OpenClaw',icon:'openclaw-color'},
  {id:'opencode',name:'opencode',icon:'opencode'},
  {id:'pi',name:'pi',icon:'pi'},
  {id:'gemini-cli',name:'Gemini CLI',icon:'gemini-color'},
  {id:'other',name:'Other',icon:null},
]

export const iconUrl: IconUrl = (icon, theme = 'light') =>
  icon?.startsWith('/') ? icon : icon ? 'https://unpkg.com/@lobehub/icons-static-png@latest/'+theme+'/'+icon+'.png' : ''

// The published setup line, kept byte-for-byte with the copy the landing pages already show.
export const command = (base: string) => 'set up treg \u2014 '+base.replace(/\/$/,'')+'/llms.txt'

// Claude.ai adds treg as a custom connector over OAuth: no setup line, no key. This link opens
// Claude.ai's add-connector dialog with this server's team MCP URL filled in: the whole surface
// (catalog, the team's own tools, media), always mounted. /mcp/v2 is the catalog-only directory one.
export const claudeConnectorLink = (base: string) =>
  'https://claude.ai/customize/connectors?modal=add-custom-connector&connectorName=Treg&connectorUrl='
  + encodeURIComponent(base.replace(/\/$/,'')+'/mcp/')

export function setupText(command: string, team?: string, token?: string, masked = false) {
  if(!team&&!token) return command
  const value = token ? (masked ? token.slice(0,14)+'••••••••••••••••' : token) : '<YOUR_TOKEN>'
  return command+'\n\nwith team '+(team||'<team-slug>')+' token: '+value
}

// `prompt` is what the copy button puts on the clipboard; `show` is the shorter line on the card.
// Order follows how often each card is copied and how many teams call the underlying tools.
export const examples: Example[] = [
  {k:'trend',cat:'Trending videos pattern', logo:'tiktok',  prompt:'Use treg to pull today\'s trending TikTok videos (video links included)'},
  {k:'enr',  cat:'Get contact emails',      logo:'people', avatar:'https://pbs.twimg.com/profile_images/1131851609774985216/OcsssQ9J_400x400.png', prompt:'Use treg to find the work email of Peter Steinberger'},
  {k:'ugc',  cat:'Make UGC videos',         logo:'seedance',  show:'Use treg to make AI UGC videos for my product, from trending hooks to finished clips',
    prompt:'Read '+location.origin+'/skills/ugc/SKILL.md and follow it with treg to make UGC videos for my product: pull the trending TikTok and Instagram videos in my vertical, extract the hook patterns, create a character with the same vibe as a presenter I pick, generate 3-5 talking-head hook clips on Seedance 2.5, and add captions. Ask me for the product and vertical first.'},
  {k:'soc',  cat:'Scrape linkedin',         logo:'linkedin',prompt:'Use treg to look up linkedin.com/in/jasonzhoudesign'},
  {k:'posts',cat:'LinkedIn posts',          logo:'linkedin',prompt:'Use treg to pull the latest LinkedIn posts from linkedin.com/in/jasonzhoudesign and summarise what they talk about'},
  {k:'serp', cat:'Keyword volume',          logo:'google',  prompt:'Use treg to pull real monthly search volume and top related keywords worth targeting for my business'},
]
export const oauthGroups: OAuthGroup[] = [
  {label:'Post on social',      items:[{s:'x',n:'X (Twitter)'},{s:'youtube',n:'YouTube'},{s:'tiktok',n:'TikTok'},{s:'linkedin',n:'LinkedIn'},{s:'facebook',n:'Facebook Pages'},{s:'instagram',n:'Instagram'}],
                                soon:[]},
  {label:'Manage ad campaigns', items:[{s:'google-ads',n:'Google Ads'},{s:'meta-ads',n:'Meta Ads'}], soon:[]},
  {label:'SEO on your own site',items:[{s:'google-analytics',n:'Google Analytics'},{s:'google-search-console',n:'Search Console'},{s:'google-business-profile',n:'Business Profile'}], soon:[]},
]

// The chips this deployment offers: a provider left out of /oauth/providers (a paused one) loses its
// chip, and a group left empty goes with it. Before the listing arrives every chip shows.
export function listedOauthGroups(listing: { service: string }[] | null | undefined): OAuthGroup[] {
  if (!listing || !listing.length) return oauthGroups
  const offered = new Set(listing.map(p => p.service))
  return oauthGroups
    .map(g => ({ ...g, items: g.items.filter(p => offered.has(p.s)) }))
    .filter(g => g.items.length)
}

// A logo that fails to load leaves its slot instead of a broken-image glyph.
export function hideBrokenImage(event: Event) {
  (event.target as HTMLElement).style.visibility = 'hidden'
}
