import { expect, test, vi } from 'vitest'
import { ApiError } from '../src/api'
import connections from '../src/state/connections.js'

test('YouTube quota exhaustion gets customer-facing copy without the upstream error', async () => {
  const vm: any = {
    resPick: null,
    connErr: '',
    connProvider: () => ({ resource_label: 'channel', resource_plural: 'channels' }),
    api: vi.fn().mockRejectedValue(new ApiError(502, { code: 'youtube_quota_exceeded' })),
  }

  await connections.openResources.call(vm, { id: 12, provider: 'youtube' })

  expect(vm.resPick.loading).toBe(false)
  expect(vm.resPick.err).toContain('We’ve requested a higher limit')
  expect(vm.resPick.err).not.toContain('403')
  expect(vm.resPick.err).not.toContain('quotaExceeded')
})
