import { describe, expect, it } from 'vitest'
import { diffFiles } from '../src/vibe/diff'
import { plain } from '../src/vibe/errors'
import { fileOf, lineOf } from '../src/vibe/fieldLine'
import { render } from '../src/vibe/markdown'

describe('the agent\'s Markdown', () => {
  it('escapes raw HTML and drops script links', () => {
    const html = render('<img src=x onerror=alert(1)> [x](javascript:alert(1)) **bold**')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('href="javascript')
    expect(html).toContain('<strong>bold</strong>')
  })
  it('keeps links on this site and never lets one point off it', () => {
    expect(render('[ok](/apps/t/x)')).toContain('href="/apps/t/x"')
    const html = render('[a](//evil.com) [b](/\\evil.com)')
    expect(html).not.toMatch(/href="\/\/|href="\/\\/)          // never a link a browser reads as another host
    expect(html).not.toContain('href="//evil.com"')
  })
  it('opens links in a new tab and gives code blocks a copy button', () => {
    const html = render('[treg](https://treg.to)\n\n```js\nlet a = "<b>"\n```')
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer"')
    expect(html).toContain('class="md-copy"')
    expect(html).toContain('&lt;b&gt;')
  })
})

describe('diffs between versions', () => {
  it('counts lines per file and keeps a little context', () => {
    const before = { readme: 'a\nb\nc\nd\ne\nf\ng\nh\ni\nj', manifest: { name: 'x' } }
    const after = { readme: 'a\nb\nc\nd\ne\nf\ng\nh\ni\nJ', manifest: { name: 'x' } }
    const [d] = diffFiles(before, after)
    expect(d.file).toBe('README.md')
    expect([d.added, d.removed]).toEqual([1, 1])
    expect(d.lines[0]).toEqual({ kind: 'gap', text: '6 unchanged lines' })
  })
  it('says nothing for unchanged files', () => {
    expect(diffFiles({ script: 'x' }, { script: 'x' })).toEqual([])
  })
})

describe('a refusal points at its file and line', () => {
  const recipe = JSON.stringify({ name: 'x', uses: ['a.b'], steps: [{ call: 'a.b' }, { call: 'c.d' }] }, null, 2)
  it('maps fields to files', () => {
    expect(fileOf('steps[1].call')).toBe('manifest')
    expect(fileOf('check.fields')).toBe('check')
    expect(fileOf('script')).toBe('script')
  })
  it('finds the line of the last key on the path', () => {
    const line = lineOf(recipe, 'steps[1].call')!
    expect(recipe.split('\n')[line - 1]).toContain('"call"')
    expect(lineOf(recipe, 'nothing.here')).toBeNull()
  })
})

describe('failures in plain words', () => {
  it('names a low balance and a refused file', () => {
    expect(plain(402, { error: 'insufficient_balance' }).title).toBe('Not enough balance')
    const p = plain(422, { error: 'manifest_invalid', field: 'uses[0]', rule: 'unknown tool' })
    expect(p.text).toBe('uses[0]: unknown tool')
    expect(p.fixable).toBe(true)
  })
  it('blames the network, not the tool, for a DNS or connection failure', () => {
    const p = plain(424, 'Error: All Reddit providers failed: x (502: upstream host resolves to a non-public address)')
    expect(p.title).toBe('Network problem')
    expect(p.fixable).toBe(false)
  })
})
