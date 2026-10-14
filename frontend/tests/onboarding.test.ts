import { readFileSync } from 'node:fs'
import { expect, test } from 'vitest'
import { extract } from '../src/onboarding/extract'

// Trimmed real answers (names, handles and post texts replaced) from the first-task calls.
const fx = (name: string) => JSON.parse(readFileSync(new URL(`./fixtures/onboarding/${name}.json`, import.meta.url), 'utf8'))

const INPUT: Record<string, string> = {
  person: 'Karri Saarinen at linear.app', people: 'linear.app', company: 'linear.app', keywords: 'issue tracking software',
  serp: 'best issue tracking tool for startups', maps: 'coffee shops in Austin, TX', social: 'Linear',
  videos: 'productivity app', scrape: 'linear.app/pricing',
}

test('every task\'s real answer becomes its view; an empty one gives none, never a crash', () => {
  for (const [view, input] of Object.entries(INPUT)) {
    expect(extract(view, fx(view), input)).toMatchObject({ view })
    expect(extract(view, [{ output: {} }, null], input)).toBeNull()
  }
})
