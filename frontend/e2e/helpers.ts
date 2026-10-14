import { expect, type Page, type Route } from '@playwright/test'
import tool from './fixtures/hub-tool.json' with { type: 'json' }
import health from './fixtures/hub-health.json' with { type: 'json' }
import run from './fixtures/hub-run.json' with { type: 'json' }

export const json = (body: unknown) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

/** A fresh verified user with one team, landed on the dashboard. */
export async function signIn(page: Page, who = 'browser', team = 'Browser test team') {
  await page.goto('/app?ref=frontend-test')
  await page.getByPlaceholder('you@work.com').fill(`${who}-${Date.now()}@example.com`)
  await page.getByRole('button', { name: 'Email me a sign-in code' }).click()
  const code = await page.getByText(/dev code \d{6}/).innerText()
  await page.getByPlaceholder('6-digit code').fill(code.match(/\d{6}/)![0])
  await page.getByRole('dialog', { name: 'Sign in' }).getByRole('button', { name: 'Sign in', exact: true }).click()
  await page.getByPlaceholder('Team name, e.g. Superdesign').fill(team)
  await page.getByRole('button', { name: 'Create team →', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Set up your team' }).getByText('Which agent are you using?', { exact: true })).toBeVisible()
  await page.getByRole('link', { name: 'Skip', exact: true }).click()
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible()
}

/** The browser-test server runs with the hub off; this answers its routes with shapes captured live. */
export async function hubOn(page: Page) {
  await page.route('**/meta', async route => route.fulfill(json({ ...(await (await route.fetch()).json()), hub: true })))
  await page.route('**/hub/tools/mine', route => route.fulfill(json([tool])))
  await page.route('**/hub/tools/*/earnings*', route => route.fulfill(json(
    { tool_id: tool.tool_id, days: 90, earned_micro: 0, runs: 0, avg_price_micro: 0, by_day: [] })))
  await page.route('**/hub/tools/*/health', route => route.fulfill(json(health)))
  await page.route('**/hub/runs/*', route => route.fulfill(json(run)))
}

/** The browser-test server sells no balance; this makes its real /billing answer say it does. */
export async function billingOn(page: Page) {
  await page.route('**/billing', async (route: Route) =>
    route.fulfill(json({ ...(await (await route.fetch()).json()), configured: true })))
}

export async function openTopUp(page: Page) {
  await page.goto('/app#orgs')
  await page.getByRole('button', { name: 'Billing', exact: true }).click()
  await page.getByRole('button', { name: 'Top up', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Top up credits' })).toBeVisible()
}

export const hubTool = tool
export const hubRun = run

/** The page never scrolls sideways: wide tables and code scroll inside themselves, the rest wraps. */
export const fitsWidth = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)

/** What makes the page wider than the window: elements past the right edge that no ancestor clips. */
export async function sidewaysCulprits(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const W = innerWidth, out: string[] = []
    for (const el of document.querySelectorAll<HTMLElement>('body *')) {
      const r = el.getBoundingClientRect()
      if (r.right <= W + 1 || !el.getClientRects().length) continue
      let clipped = false
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement)
        if (getComputedStyle(p).overflowX !== 'visible') { clipped = true; break }
      if (!clipped) out.push(`<${el.tagName.toLowerCase()} class="${el.className}"> ends at ${Math.round(r.right)}px`)
    }
    return out.slice(0, 8)
  })
}

/**
 * Text that paints over other text, or past the edge of its row, inside every element `rows` matches.
 * Each text run is measured where it is actually painted (a Range, not its element's box, so text that
 * refuses to wrap and spills out of a narrow cell is caught), clipped by any ancestor that clips it,
 * so an ellipsis-truncated line counts only as far as it shows.
 */
export async function textCollisions(page: Page, rows: string): Promise<string[]> {
  return page.evaluate((selector) => {
    const clip = (r: DOMRect, node: Node, row: Element) => {
      let { left, right, top, bottom } = r
      for (let p = node.parentElement; p && p !== row; p = p.parentElement) {
        const cs = getComputedStyle(p)
        if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue
        const c = p.getBoundingClientRect()
        left = Math.max(left, c.left); right = Math.min(right, c.right); top = Math.max(top, c.top); bottom = Math.min(bottom, c.bottom)
      }
      return { left, right, top, bottom }
    }
    const found: string[] = []
    for (const row of document.querySelectorAll(selector)) {
      if (!row.getClientRects().length) continue
      const edge = row.getBoundingClientRect()
      const runs: { node: Node, text: string, box: { left: number, right: number, top: number, bottom: number } }[] = []
      const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT)
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const text = node.textContent!.trim()
        if (!text || !node.parentElement?.getClientRects().length) continue
        const range = document.createRange(); range.selectNodeContents(node)
        for (const r of range.getClientRects()) {
          const box = clip(r, node, row)
          if (box.right - box.left < 1 || box.bottom - box.top < 1) continue
          runs.push({ node, text: text.slice(0, 48), box })
          if (box.right > edge.right + 1 || box.left < edge.left - 1) found.push(`past its row: ${text.slice(0, 48)}`)
        }
      }
      for (let i = 0; i < runs.length; i++) for (let j = i + 1; j < runs.length; j++) {
        const a = runs[i]!, b = runs[j]!
        if (a.node === b.node) continue
        if (a.box.right - 1 > b.box.left && b.box.right - 1 > a.box.left && a.box.bottom - 1 > b.box.top && b.box.bottom - 1 > a.box.top)
          found.push(`${a.text} / ${b.text}`)
      }
    }
    return [...new Set(found)]
  }, rows)
}

// PostHog stood in by a stub that records what the page captures (`__ph.events`).
export async function stubPostHog(page: Page) {
  await page.addInitScript(() => {
    const w = window as any
    w.__ph = { events: [] as [string, any][] }
    w.posthog = {
      onFeatureFlags() {},
      register() {}, unregister() {},
      capture(name: string, props: any) { w.__ph.events.push([name, props]) },
    }
  })
}
