import { expect, test } from 'vitest'
import { takeTicket } from '../src/state/tickets.js'

test('a newer call of the same loader makes the older answer stale', () => {
  const counters = {}
  const first = takeTicket(counters, 'tools')
  const other = takeTicket(counters, 'billing')
  expect(first()).toBe(true)
  const second = takeTicket(counters, 'tools')
  expect(first()).toBe(false)
  expect(second()).toBe(true)
  expect(other()).toBe(true)
})

test('a team switch makes a team-scoped answer stale', () => {
  let team = 'a'
  const ticket = takeTicket({}, 'members', () => team)
  expect(ticket()).toBe(true)
  team = 'b'
  expect(ticket()).toBe(false)
})
