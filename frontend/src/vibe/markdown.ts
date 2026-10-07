// The agent's replies as Markdown. Raw HTML is off (markdown-it escapes it), links open in a new tab
// and only http(s) and mailto survive, and every code block carries a Copy button the page wires up.
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({ html: false, linkify: true, breaks: false, typographer: false })

// Web links, mail, and paths on this site; never `//host` or `/\\host`, which browsers read as another site.
md.validateLink = (url: string) => /^(https?:|mailto:|\/(?![/\\])|#)/i.test(url.trim())

const defaultLink = md.renderer.rules.link_open || ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return defaultLink(tokens, idx, options, env, self)
}

md.renderer.rules.fence = (tokens, idx) => {
  const t = tokens[idx]
  const lang = md.utils.escapeHtml((t.info || '').trim().split(/\s+/)[0] || '')
  return `<div class="md-code"><div class="md-code-bar"><span>${lang}</span><button type="button" class="md-copy">Copy</button></div>`
    + `<pre><code>${md.utils.escapeHtml(t.content)}</code></pre></div>`
}

export function render(text: string): string {
  return md.render(text || '')
}

// Event delegation for the Copy buttons inside rendered Markdown.
export async function onCopyClick(e: MouseEvent): Promise<void> {
  const btn = (e.target as HTMLElement).closest('.md-copy') as HTMLButtonElement | null
  if (!btn) return
  const code = btn.closest('.md-code')?.querySelector('code')?.textContent || ''
  try { await navigator.clipboard.writeText(code); btn.textContent = 'Copied'; setTimeout(() => (btn.textContent = 'Copy'), 1400) } catch { /* no clipboard */ }
}
