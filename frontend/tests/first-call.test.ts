import { afterAll, afterEach, beforeEach, expect, test, vi } from 'vitest'
import { markRaw, reactive } from 'vue'
import onboarding from '../src/state/onboarding.js'
import computed from '../src/state/onboardingComputed.js'

vi.mock('../src/state/storage.js', () => ({ storageGet: vi.fn(), storageRemove: vi.fn() }))
vi.hoisted(() => vi.stubGlobal('location', { origin: 'http://localhost' }))
afterAll(() => vi.unstubAllGlobals())

function page(api: any) {
  return reactive({
    ...onboarding, api, elements: markRaw({}), view: 'start', sessionMode: true, activeOrgId: 1,
    startProgress: null, startWatching: false,
    get startStage() { return computed.startStage.call(this) },
  }) as any
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

test('Getting started moves from waiting to connected to the first call, then stops', async () => {
  const api = vi.fn()
    .mockResolvedValueOnce({ connected: false, first_call: null })
    .mockResolvedValueOnce({ connected: true, first_call: null })
    .mockResolvedValueOnce({ connected: true, first_call: { tool: 'alpha', at: '2026-11-09T00:00:00' } })
  const vm = page(api)
  vm.pollFirstCall()
  await vi.advanceTimersByTimeAsync(0)
  expect(vm.startStage).toBe('waiting')
  expect(vm.startWatching).toBe(true)
  await vi.advanceTimersByTimeAsync(4000)
  expect(vm.startStage).toBe('connected')
  await vi.advanceTimersByTimeAsync(4000)
  expect(vm.startStage).toBe('first')
  expect(vm.startProgress.first_call.tool).toBe('alpha')
  expect(vm.startWatching).toBe(false)
  await vi.advanceTimersByTimeAsync(60000)
  expect(api).toHaveBeenCalledTimes(3)
  expect(api.mock.calls[0][0]).toBe('/orgs/1/first-call')
  expect(api.mock.calls[0][1].cache).toBe('no-store')
})

test('leaving the page or switching teams stops the poll; the round is bounded', async () => {
  const api = vi.fn().mockResolvedValue({ connected: false, first_call: null })
  const vm = page(api)
  vm.pollFirstCall()
  await vi.advanceTimersByTimeAsync(8000)
  expect(api).toHaveBeenCalledTimes(3)
  vm.view = 'catalog'
  await vi.advanceTimersByTimeAsync(20000)
  expect(api).toHaveBeenCalledTimes(3)
  expect(vm.startWatching).toBe(false)

  vm.view = 'start'
  vm.pollFirstCall()
  vm.activeOrgId = 2
  expect(vm.startStage).toBeNull()  // the old team's answer is never shown for another team
  api.mockClear()
  await vi.advanceTimersByTimeAsync(20000)
  expect(api).not.toHaveBeenCalled()
  expect(vm.startStage).toBeNull()

  api.mockClear()
  vm.pollFirstCall()
  await vi.advanceTimersByTimeAsync(10 * 60 * 1000)
  expect(api).toHaveBeenCalledTimes(75)
  expect(vm.startWatching).toBe(false)
  expect(vm.startStage).toBe('waiting')
})

test('a team that already made a call is answered once, with no polling', async () => {
  const api = vi.fn().mockResolvedValue({ connected: true, first_call: { tool: null, at: '2026-11-01T00:00:00' } })
  const vm = page(api)
  vm.pollFirstCall()
  await vi.advanceTimersByTimeAsync(20000)
  expect(api).toHaveBeenCalledTimes(1)
  expect(vm.startStage).toBe('first')
  vm.pollFirstCall()
  expect(api).toHaveBeenCalledTimes(1)
})
