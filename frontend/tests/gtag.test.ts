import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { expect, test } from 'vitest'

const source = readFileSync(resolve(__dirname, '../../src/treg/web/gtag.js'), 'utf8')

function timeline(conversionOnly = false) {
  const events: unknown[][] = []
  const window: any = { dataLayer: { push: (args: IArguments) => events.push([...args]) } }
  const document = {
    currentScript: { hasAttribute: (name: string) => conversionOnly && name === 'data-conversion-only' },
    createElement: () => ({}),
    head: { appendChild: (script: { src: string }) => events.push(['script', script.src]) },
  }
  runInNewContext(source, { window, document, Date })
  return { window, events }
}

test('every consent signal is denied before Google\'s tag loads or is configured', () => {
  const { events } = timeline()
  const at = (name: string) => events.findIndex(e => e[0] === name)
  expect(events[at('consent')]).toEqual(['consent', 'default', {
    ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied', analytics_storage: 'denied',
  }])
  expect(events[at('script')][1]).toMatch(/^https:\/\/www\.googletagmanager\.com\/gtag\/js\?id=AW-/)
  expect(at('consent')).toBeLessThan(at('script'))
  expect(at('script')).toBeLessThan(at('config'))
})

test('the dashboard\'s conversion-only tag sends no page view of its own', () => {
  expect(timeline(true).events.find(e => e[0] === 'config')?.[2]).toEqual({ send_page_view: false })
  expect(timeline(false).events.find(e => e[0] === 'config')?.[2]).toEqual({})
})

test('a team\'s signup converts once, keyed by the team, and never without one', () => {
  const { window, events } = timeline(true)
  window.tregSignupConversion(42)
  window.tregSignupConversion(42)
  window.tregSignupConversion('')
  const conversions = events.filter(e => e[0] === 'event' && e[1] === 'conversion')
  expect(conversions).toHaveLength(1)
  expect(conversions[0][2]).toMatchObject({ transaction_id: 'treg-web-signup-42' })
  expect((conversions[0][2] as { send_to: string }).send_to).toMatch(/^AW-\d+\/\w+$/)
})
