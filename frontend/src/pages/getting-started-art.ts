// Getting started artwork, imported so Vite hashes each file and serves it immutable from
// /app/ui/assets. Kept apart from agent-setup/data.ts, which also compiles to the Arena's classic
// script. Unmodified brand marks come from the pinned LobeHub package; the `cover-*` files in
// assets/getting-started are the designer's variants of a mark.
import grok from '@lobehub/icons-static-svg/icons/grok.svg'
import hermes from '@lobehub/icons-static-svg/icons/hermesagent.svg'
import claude from '@lobehub/icons-static-svg/icons/claude-color.svg'
import codex from '@lobehub/icons-static-svg/icons/codex-color.svg'
import gemini from '@lobehub/icons-static-svg/icons/gemini-color.svg'
import claudeCode from '../assets/getting-started/cover-claude-code.svg'
import opencode from '../assets/getting-started/cover-opencode.svg'
import pi from '../assets/getting-started/cover-pi.svg'
import cursor from '../assets/getting-started/cover-cursor.svg'
import other from '../assets/getting-started/cover-other.svg'
import trend from '../assets/getting-started/example-trend.webp'
import enr from '../assets/getting-started/example-enr.webp'
import ugc from '../assets/getting-started/example-ugc.webp'
import soc from '../assets/getting-started/example-soc.webp'
import posts from '../assets/getting-started/example-posts.webp'
import serp from '../assets/getting-started/example-serp.webp'
import ugcIcon from '../assets/getting-started/icon-ugc.svg'

// Keyed by agent id. OpenClaw keeps its poster image instead of a cover.
export const coverLogos: Record<string, string> = {
  grokbot: grok, hermes, claudeai: claude, 'claude-code': claudeCode, codex,
  opencode, pi, cursor, 'gemini-cli': gemini, other,
}

// Keyed by example `k`.
export const exampleBanners: Record<string, string> = { trend, enr, ugc, soc, posts, serp }
export const exampleIcons: Record<string, string> = { ugc: ugcIcon }
