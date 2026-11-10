import { expect, test } from 'vitest'
import { ApiError, withReportRetry } from '../src/api'
import activity from '../src/state/activity.js'

const noSleep = async () => {}

test('a refused report is retried until it answers', async () => {
  let calls = 0
  const got = await withReportRetry(async () => {
    calls++
    if (calls < 3) throw new ApiError(429, 'this report is busy')
    return 'rows'
  }, [1, 1, 1], noSleep)
  expect(got).toBe('rows')
  expect(calls).toBe(3)
})

test('the last refusal and any other failure are thrown, not swallowed', async () => {
  let calls = 0
  await expect(withReportRetry(async () => { calls++; throw new ApiError(429) }, [1, 1], noSleep))
    .rejects.toMatchObject({ status: 429 })
  expect(calls).toBe(3)
  calls = 0
  await expect(withReportRetry(async () => { calls++; throw new ApiError(500) }, [1, 1], noSleep))
    .rejects.toMatchObject({ status: 500 })
  expect(calls).toBe(1)
})

test('spend by tag loads one key at a time and shows a key that failed instead of hiding it', async () => {
  let inFlight = 0, peak = 0
  const state: any = {
    canAdmin: true, activeOrgId: 7, usageDays: 30, tagUsage: {}, tagUsageErr: {}, tagKeys: [],
    ticket: () => () => true,
    api: async () => ({ primary: 'customer', seen: ['customer', 'workspace'], budgetable: [] }),
    reportApi: async (path: string) => {
      inFlight++; peak = Math.max(peak, inFlight)
      await Promise.resolve()
      inFlight--
      if (path.includes('key=workspace')) throw new ApiError(429, 'this report is busy')
      return { rows: [], unattributed_micro: 0 }
    },
  }
  await activity.loadTagUsage.call(state)
  expect(peak).toBe(1)
  expect(Object.keys(state.tagUsage)).toEqual(['customer'])
  expect(state.tagUsageErr.workspace).toContain('this report is busy')
  expect(state.tagKeys).toEqual(['customer', 'workspace'])
})
