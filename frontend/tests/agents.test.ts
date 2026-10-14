import { afterAll, afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { markRaw, nextTick, reactive, watch } from 'vue'
import agents from '../src/state/agents.js'
import computed from '../src/state/agentsComputed.js'
import controller from '../src/state/controller.js'
import credentialIssues, { credentialIssueComputed } from '../src/state/credentialIssues.js'
import { storageSet } from '../src/state/storage.js'
import keys from '../src/state/keys.js'
import session from '../src/state/session.js'
import team from '../src/state/team.js'
import tickets from '../src/state/tickets.js'

vi.mock('../src/state/storage.js', () => ({ storageSet: vi.fn(), storageRemove: vi.fn(), storageGet: vi.fn() }))

vi.hoisted(() => vi.stubGlobal('location', { origin: 'http://localhost' }))
afterAll(() => vi.unstubAllGlobals())

const issued = (key = 7) => ({ user_id: 3, api_key_id: key, org_id: 1, name: 'bot', connected: false })
const deferred = <T = any>() => {
  let resolve!: (value: T) => void
  let reject!: (error: any) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const cleanup: (() => void)[] = []

function dashboard() {
  const vm: any = reactive({
    ...agents, ...keys, ...tickets, ...credentialIssues, switchOrg: session.switchOrg,
    elements: markRaw({}), credentialIssues: {}, activeSlug: 'team', sessionMode: true,
    view: 'orgs', canAdmin: true, orgTab: 'members',
    agents: [{ user_id: 3, connected: true }],
    agentName: 'bot', agentCap: -1, agentRole: 'member', agentAccessMode: 'all',
    projects: [], accessNames: [], agentProjSel: {},
    api: vi.fn().mockResolvedValue({ connected: false }),
    loadOrgAdmin: vi.fn().mockResolvedValue(undefined), loadApiKeys: vi.fn().mockResolvedValue(undefined),
    loadAll: vi.fn(), intercomUpdate: vi.fn(), resetRenameForm: vi.fn(), connected: () => true,
    get activeSlugNow() { return this.activeSlug },
    get activeOrg() { return { slug: this.activeSlug, org_id: this.activeSlug === 'team' ? 1 : 2 } },
    get activeOrgId() { return this.activeOrg.org_id },
    get credentialIssue() { return credentialIssueComputed.credentialIssue.call(this) },
    get newAgent() { return credentialIssueComputed.newAgent.call(this) },
    get newApiKey() { return credentialIssueComputed.newApiKey.call(this) },
    get agentConnectionTarget() { return computed.agentConnectionTarget.call(this) },
    get agentConnected() { return computed.agentConnected.call(this) },
  })
  cleanup.push(watch(() => vm.agentConnectionTarget, () => controller.watch.agentConnectionTarget.call(vm)))
  cleanup.push(watch(() => vm.activeOrgId, (id, previous) => controller.watch.activeOrgId.call(vm, id, previous)))
  cleanup.push(() => vm.clearCredentialIssues())
  return vm
}

function ready(vm: any, result = issued()) {
  vm.credentialIssues[vm.activeOrgId] = { org_id: vm.activeOrgId, org: vm.activeSlugNow, type: 'agent', status: 'ready', result }
}

beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks() })
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers() })

test('initial Team loading still fetches the full roster; waiting polls only the new key', async () => {
  const vm = dashboard()
  vm.api.mockResolvedValue([])
  vm.loadApiKeys = keys.loadApiKeys
  await team.loadOrgAdmin.call(vm)
  expect(vm.api).toHaveBeenCalledTimes(9)
  expect(vm.api.mock.calls.map(([path]: [string]) => path)).toEqual(expect.arrayContaining([
    '/orgs/1/members', '/orgs/1/agents', '/orgs/1/agents/observed', '/orgs/1/api-keys/spend',
  ]))
  vm.api.mockClear().mockResolvedValue({ connected: false })
  ready(vm)
  await nextTick()
  await vi.advanceTimersByTimeAsync(9000)
  expect(vm.api).toHaveBeenCalledTimes(3)
  for (const [path, options] of vm.api.mock.calls) {
    expect(path).toBe('/orgs/1/agents/3/connection?api_key_id=7')
    expect(options.signal).toBeInstanceOf(AbortSignal)
    expect(options.cache).toBe('no-store')
  }
  expect(vm.loadOrgAdmin).not.toHaveBeenCalled()
})

test.each(['create', 'rotate'] as const)('%s refreshes once and detects only the issued credential', async action => {
  const vm = dashboard()
  vm.api.mockResolvedValueOnce({ ...issued(), token: 'test-token' }).mockResolvedValueOnce({ connected: false }).mockResolvedValue({ connected: true })
  if (action === 'create') await vm.createAgent()
  else await vm.rotateAgent({ user_id: 3, name: 'bot', role: 'member', daily_call_cap: -1 }, true)
  await nextTick()
  expect(vm.loadOrgAdmin).toHaveBeenCalledTimes(1)
  expect(vm.agentConnected).toBe(false) // Lifetime roster history must not satisfy a new key.
  await vi.advanceTimersByTimeAsync(3000)
  expect(vm.agentConnected).toBe(true)
  await vi.advanceTimersByTimeAsync(120000)
  expect(vm.api).toHaveBeenCalledTimes(3)
})

test('the API Keys rotation card uses the replacement key id and stops when dismissed', async () => {
  const vm = dashboard()
  vm.api.mockResolvedValueOnce({ id: 8, secret: 'replacement' }).mockResolvedValue({ connected: false })
  await vm.keyAction({ id: 7, user_id: 3, kind: 'agent', assigned_type: 'agent', assigned_name: 'bot' }, 'rotate')
  await nextTick()
  expect(vm.loadApiKeys).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(3000)
  expect(vm.api).toHaveBeenLastCalledWith('/orgs/1/agents/3/connection?api_key_id=8', expect.anything())
  const signal = vm.api.mock.lastCall[1].signal
  vm.dismissCredentialIssue()
  await nextTick()
  expect(signal.aborted).toBe(true)
  await vi.advanceTimersByTimeAsync(9000)
  expect(vm.api).toHaveBeenCalledTimes(3)
})

test('slow responses never overlap, even when resume is called again', async () => {
  const vm = dashboard(), response = deferred()
  vm.api.mockReturnValue(response.promise)
  ready(vm)
  await nextTick()
  await vi.advanceTimersByTimeAsync(3000)
  vm.resumeAgentPoll()
  await vi.advanceTimersByTimeAsync(60000)
  expect(vm.api).toHaveBeenCalledTimes(1)
  response.resolve({ connected: false })
  await vi.advanceTimersByTimeAsync(2999)
  expect(vm.api).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(vm.api).toHaveBeenCalledTimes(2)
})

test('leaving aborts the pending request; returning resumes and ignores the old success', async () => {
  const vm = dashboard(), old = deferred()
  vm.api.mockReturnValueOnce(old.promise).mockResolvedValue({ connected: false })
  ready(vm)
  await nextTick()
  await vi.advanceTimersByTimeAsync(3000)
  const signal = vm.api.mock.lastCall[1].signal
  vm.view = 'catalog'
  vm.stopAgentPoll() // TeamPage.beforeUnmount
  expect(signal.aborted).toBe(true)
  await vi.advanceTimersByTimeAsync(30000)
  expect(vm.api).toHaveBeenCalledTimes(1)
  vm.view = 'orgs'
  vm.resumeAgentPoll() // TeamPage.mounted
  old.resolve({ connected: true }) // Also safe with a transport that ignores abort.
  await vi.advanceTimersByTimeAsync(3000)
  expect(vm.agentConnected).toBe(false)
  expect(vm.api).toHaveBeenCalledTimes(2)
  vm.api.mockResolvedValue({ connected: true })
  await vi.advanceTimersByTimeAsync(3000)
  expect(vm.agentConnected).toBe(true)
})

test('switching org cancels immediately and ignores stale responses even after switching back', async () => {
  const vm = dashboard(), old = deferred()
  vm.api.mockReturnValueOnce(old.promise).mockResolvedValue({ connected: false })
  ready(vm)
  await nextTick()
  await vi.advanceTimersByTimeAsync(3000)
  const signal = vm.api.mock.lastCall[1].signal
  vm.switchOrg({ slug: 'other' })
  expect(signal.aborted).toBe(true)
  vm.activeSlug = 'other'
  await nextTick()
  vm.switchOrg({ slug: 'team' })
  vm.activeSlug = 'team'
  await nextTick()
  old.resolve({ connected: true })
  await vi.advanceTimersByTimeAsync(9000)
  expect(vm.newAgent?.api_key_id).toBe(7)
  expect(vm.agentConnected).toBe(false)
  expect(vm.api.mock.calls.filter(([path]: [string]) => path.includes('/connection?'))).toHaveLength(5)
})

test('automatic active-org changes cancel a poll without switchOrg', async () => {
  const vm = dashboard(), response = deferred()
  vm.api.mockReturnValue(response.promise)
  ready(vm)
  await nextTick()
  await vi.advanceTimersByTimeAsync(3000)
  const signal = vm.api.mock.lastCall[1].signal
  vm.activeSlug = 'other'
  await nextTick()
  expect(signal.aborted).toBe(true)
  response.resolve({ connected: true })
  await vi.advanceTimersByTimeAsync(6000)
  expect(vm.agentConnected).toBe(false)
  expect(vm.api).toHaveBeenCalledTimes(1)
})

test('a replaced card is not updated by the previous request or stopped by its completion', async () => {
  const vm = dashboard(), old = deferred()
  vm.api.mockReturnValueOnce(old.promise).mockResolvedValue({ connected: false })
  ready(vm)
  await nextTick()
  await vi.advanceTimersByTimeAsync(3000)
  const signal = vm.api.mock.lastCall[1].signal
  ready(vm, issued(8))
  await nextTick()
  expect(signal.aborted).toBe(true)
  old.reject(new DOMException('Aborted', 'AbortError'))
  await vi.advanceTimersByTimeAsync(6000)
  expect(vm.agentConnected).toBe(false)
  expect(vm.api).toHaveBeenCalledTimes(3)
  expect(vm.api).toHaveBeenLastCalledWith('/orgs/1/agents/3/connection?api_key_id=8', expect.anything())
})

test.each([401, 403, 404])('status %s stops a no-longer-authorized or removed key', async status => {
  const vm = dashboard()
  vm.api.mockRejectedValue({ status })
  ready(vm)
  await nextTick()
  await vi.advanceTimersByTimeAsync(120000)
  expect(vm.api).toHaveBeenCalledTimes(1)
  expect(vm.agentConnected).toBe(false)
})

test.each([false, true])('unconnected/error polling is bounded at 40 attempts (error=%s)', async error => {
  const vm = dashboard()
  if (error) vm.api.mockRejectedValue({ status: 503 })
  ready(vm)
  await nextTick()
  await vi.advanceTimersByTimeAsync(240000)
  expect(vm.api).toHaveBeenCalledTimes(40)
  expect(vm.elements.agentPoll).toBeNull()
})

const actions = ['create', 'rotate', 'keyAction'] as const
type IssueAction = typeof actions[number]
const agent = { user_id: 3, name: 'bot', role: 'member', daily_call_cap: -1 }
const key = { id: 7, user_id: 3, kind: 'agent', assigned_type: 'agent', assigned_name: 'bot' }
const responseFor = (action: IssueAction, id = 8) => action === 'keyAction'
  ? { id, secret: `test-secret-${id}` }
  : { ...issued(id), token: `test-secret-${id}` }
function issue(vm: any, action: IssueAction, keyId = 7) {
  vm.agentAccessMode = 'all'; vm.agentName = 'bot'
  return action === 'create' ? vm.createAgent() : action === 'rotate'
    ? vm.rotateAgent(agent, true) : vm.keyAction({ ...key, id: keyId }, 'rotate')
}
const shown = (vm: any) => vm.newAgent?.token || vm.newApiKey?.secret
const posts = (vm: any) => vm.api.mock.calls.filter(([, options]: [string, any]) => options?.method === 'POST')
async function switchTeam(vm: any, slug: string) {
  vm.switchOrg({ slug }); await nextTick()
}

describe('key actions blocked by a retained issuance', () => {
  test.each(['disable', 'enable', 'revoke', 'hide', 'rotate'])('%s reports the guard at both request and execution boundaries', async action => {
    const vm = dashboard()
    ready(vm)
    vm.keyMenu = { key }
    vm.keyConfirm = null
    vm.requestKeyAction(key, action)
    expect(vm.keyConfirm).toBeNull()
    expect(vm.keyMenu).not.toBeNull()
    expect(vm.keyErr).toBe(vm.keyActionBlockReason())
    expect(vm.keyErr).toContain('retained key result')
    vm.keyErr = ''
    await vm.keyAction(key, action)
    expect(vm.keyErr).toBe(vm.keyActionBlockReason())
    expect(vm.api).not.toHaveBeenCalled()
    expect(vm.loadApiKeys).not.toHaveBeenCalled()
  })

  test.each(['disable', 'revoke', 'hide'])('%s keeps an open confirmation when an issuance appears and requires a fresh confirmation', async action => {
    const vm = dashboard()
    vm.requestKeyAction(key, action)
    const confirmation = vm.keyConfirm
    ready(vm)
    await vm.confirmKeyAction()
    expect(vm.keyConfirm).toBe(confirmation)
    expect(vm.keyErr).toContain('retained key result')
    expect(vm.api).not.toHaveBeenCalled()
    vm.dismissCredentialIssue() // the existing explicit acknowledgement, not an automatic bypass
    await nextTick()
    expect(vm.api).not.toHaveBeenCalled()
    expect(vm.keyConfirm).toBe(confirmation)
    await vm.confirmKeyAction()
    expect(vm.keyConfirm).toBeNull()
    expect(posts(vm)).toEqual([[`/orgs/1/api-keys/7/${action}`, { method: 'POST' }]])
    expect(vm.keyErr).toBe('')
  })
})

describe('credential results survive navigation without repeating the write', () => {
  test.each(actions)('%s survives switching away and back while its POST is pending', async action => {
    const vm = dashboard(), response = deferred()
    vm.api.mockReturnValueOnce(response.promise).mockResolvedValue({ connected: false })
    const done = issue(vm, action)
    await switchTeam(vm, 'other')
    expect(shown(vm)).toBeUndefined()
    await switchTeam(vm, 'team')
    expect(vm.credentialIssue.status).toBe('pending')
    await issue(vm, action) // returning does not unlock a duplicate rotation
    response.resolve(responseFor(action))
    await done; await nextTick()
    expect(shown(vm)).toBe('test-secret-8')
    expect(posts(vm)).toHaveLength(1)
    expect(posts(vm)[0][1].signal).toBeUndefined() // aborting a read is never cancelling a write
    expect(vm.credentialIssue.org_id).toBe(1)
    expect(JSON.stringify(vi.mocked(storageSet).mock.calls)).not.toContain('test-secret')
    vm.dismissCredentialIssue()
    expect(vm.credentialIssues[1]).toBeUndefined()
    expect(JSON.stringify(vm.credentialIssues)).not.toContain('test-secret')
    await switchTeam(vm, 'other'); await switchTeam(vm, 'team')
    expect(shown(vm)).toBeUndefined()
    expect(posts(vm)).toHaveLength(1)
  })

  test.each(actions)('%s keeps a late result under its origin while another org issues a key', async action => {
    const vm = dashboard(), old = deferred()
    vm.api.mockImplementation((path: string, options: any) => options?.method === 'POST'
      ? path.startsWith('/orgs/1/') ? old.promise : Promise.resolve(responseFor(action, 9))
      : Promise.resolve({ connected: false }))
    const first = issue(vm, action)
    await switchTeam(vm, 'other')
    await issue(vm, action)
    expect(shown(vm)).toBe('test-secret-9')
    old.resolve(responseFor(action))
    await first; await nextTick()
    expect(shown(vm)).toBe('test-secret-9')
    expect(vm.credentialIssues[1].result.token || vm.credentialIssues[1].result.secret).toBe('test-secret-8')
    expect(vm.credentialIssues[1].status).toBe('held')
    await switchTeam(vm, 'team'); await vi.advanceTimersByTimeAsync(0)
    expect(shown(vm)).toBe('test-secret-8')
    expect(vm.credentialIssue.org_id).toBe(1)
    expect(posts(vm)).toHaveLength(2) // exactly one user-requested POST per org
  })

  test.each(actions)('%s finishing off-page is retained and revalidated on return', async action => {
    const vm = dashboard(), response = deferred()
    vm.api.mockReturnValueOnce(response.promise).mockResolvedValue({ connected: false })
    const done = issue(vm, action)
    vm.view = 'catalog'; vm.parkCredentialIssue()
    response.resolve(responseFor(action)); await done; await nextTick()
    await vi.advanceTimersByTimeAsync(9000)
    expect(vm.api).toHaveBeenCalledTimes(1)
    expect(vm.credentialIssue.status).toBe('held')
    expect(shown(vm)).toBeUndefined()
    vm.view = 'orgs'; await vm.resumeCredentialIssue(); await nextTick()
    expect(shown(vm)).toBe('test-secret-8')
    await vi.advanceTimersByTimeAsync(3000)
    expect(vm.api).toHaveBeenCalledTimes(3)
    expect(posts(vm)).toHaveLength(1)
  })

  test.each(actions)('%s blocks every issuance entry until its result is acknowledged', async action => {
    const vm = dashboard(), response = deferred()
    vm.api.mockReturnValueOnce(response.promise).mockResolvedValue({ connected: false })
    const done = issue(vm, action)
    for (const next of actions) await issue(vm, next)
    response.resolve(responseFor(action)); await done
    for (const next of actions) await issue(vm, next)
    expect(posts(vm)).toHaveLength(1)
    expect(shown(vm)).toBe('test-secret-8')
    vm.dismissCredentialIssue()
    vm.api.mockResolvedValueOnce(responseFor('keyAction', 9)).mockResolvedValue({ connected: false })
    await issue(vm, 'keyAction', 8)
    expect(posts(vm)).toHaveLength(2)
    expect(posts(vm)[1][0]).toBe('/orgs/1/api-keys/8/rotate')
    expect(shown(vm)).toBe('test-secret-9')
    expect(JSON.stringify(vm.credentialIssues)).not.toContain('test-secret-8')
  })

  test.each(actions)('%s does not reveal a key already replaced before its delayed response arrives', async action => {
    const vm = dashboard(), response = deferred()
    vm.api.mockReturnValueOnce(response.promise).mockRejectedValue({ status: 404 })
    const done = issue(vm, action)
    await switchTeam(vm, 'other')
    response.resolve(responseFor(action)); await done
    await switchTeam(vm, 'team'); await vi.advanceTimersByTimeAsync(0)
    expect(vm.credentialIssue.status).toBe('unavailable')
    expect(shown(vm)).toBeUndefined()
    expect(JSON.stringify(vm.credentialIssues)).not.toContain('test-secret')
    expect(posts(vm)).toHaveLength(1)
  })

  test.each(actions)('%s preserves the result when a restore check fails and retries only the read', async action => {
    const vm = dashboard()
    vm.api.mockResolvedValueOnce(responseFor(action)).mockRejectedValueOnce({ status: 503 })
      .mockResolvedValue({ connected: false })
    await issue(vm, action)
    expect(vm.credentialIssue.status).toBe('held')
    expect(vm.credentialIssue.error).toContain('saved in this tab')
    expect(shown(vm)).toBeUndefined()
    await vm.resumeCredentialIssue()
    expect(shown(vm)).toBe('test-secret-8')
    expect(posts(vm)).toHaveLength(1)
  })

  test('an aborted restore cannot discard the retained result or overwrite a newer restore', async () => {
    const vm = dashboard(), old = deferred()
    vm.api.mockResolvedValueOnce(responseFor('create')).mockReturnValueOnce(old.promise)
      .mockResolvedValue({ connected: false })
    const done = issue(vm, 'create')
    await nextTick()
    const signal = vm.api.mock.lastCall[1].signal
    await switchTeam(vm, 'other')
    expect(signal.aborted).toBe(true)
    await switchTeam(vm, 'team'); await vi.advanceTimersByTimeAsync(0)
    expect(shown(vm)).toBe('test-secret-8')
    old.reject({ status: 404 }) // even a transport ignoring abort cannot erase a newer result
    await done
    expect(shown(vm)).toBe('test-secret-8')
    expect(posts(vm)).toHaveLength(1)
  })

  test('a late previous poll cannot erase a subsequent, acknowledged rotation', async () => {
    const vm = dashboard(), old = deferred()
    vm.api.mockResolvedValueOnce(responseFor('create')).mockResolvedValueOnce({ connected: false })
      .mockReturnValueOnce(old.promise).mockResolvedValue({ connected: false })
    await issue(vm, 'create'); await nextTick(); await vi.advanceTimersByTimeAsync(3000)
    vm.dismissCredentialIssue()
    vm.api.mockResolvedValueOnce(responseFor('rotate', 9))
    await issue(vm, 'rotate')
    old.reject({ status: 404 }); await vi.advanceTimersByTimeAsync(0)
    expect(shown(vm)).toBe('test-secret-9')
    expect(posts(vm)).toHaveLength(2)
  })

  test('a key revoked after check-in is checked again before displaying it on return', async () => {
    const vm = dashboard()
    vm.api.mockResolvedValueOnce(responseFor('rotate')).mockResolvedValue({ connected: true })
    await issue(vm, 'rotate')
    expect(vm.agentConnected).toBe(true)
    vm.view = 'catalog'; vm.parkCredentialIssue()
    vm.api.mockRejectedValue({ status: 404 })
    vm.view = 'orgs'; await vm.resumeCredentialIssue()
    expect(vm.credentialIssue.status).toBe('unavailable')
    expect(shown(vm)).toBeUndefined()
    expect(posts(vm)).toHaveLength(1)
  })

  test('a failed write reports uncertainty in its own org and never retries automatically', async () => {
    const vm = dashboard(), response = deferred()
    vm.api.mockReturnValueOnce(response.promise)
    const done = issue(vm, 'rotate')
    await switchTeam(vm, 'other')
    response.reject({ status: 503 }); await done
    expect(vm.credentialIssue).toBeNull()
    await switchTeam(vm, 'team'); await vi.advanceTimersByTimeAsync(120000)
    expect(vm.credentialIssue.status).toBe('failed')
    expect(vm.credentialIssue.error).toContain('may have completed')
    expect(posts(vm)).toHaveLength(1)
  })

  test('app teardown drops all secrets and ignores a subsequent write response', async () => {
    const vm = dashboard(), response = deferred()
    vm.api.mockReturnValueOnce(response.promise)
    const done = issue(vm, 'create')
    vm.clearCredentialIssues()
    response.resolve(responseFor('create')); await done
    expect(vm.credentialIssues).toEqual({})
    expect(shown(vm)).toBeUndefined()
  })

  test('additional key creation uses the same acknowledgement and return boundary', async () => {
    const vm = dashboard()
    vm.keyName = 'Extra'
    vm.api.mockResolvedValueOnce({ id: 8, kind: 'additional_human', secret: 'test-secret-8' })
      .mockResolvedValue([{ id: 8, state: 'active' }])
    await vm.createApiKey()
    expect(shown(vm)).toBe('test-secret-8')
    await switchTeam(vm, 'other'); await switchTeam(vm, 'team'); await vi.advanceTimersByTimeAsync(0)
    expect(shown(vm)).toBe('test-secret-8')
    expect(posts(vm)).toHaveLength(1)
  })

  test('a Default rotation from another session is not presented as the current token', async () => {
    const vm = dashboard()
    vm.api.mockResolvedValueOnce({ id: 7, secret: 'old-default' })
      .mockResolvedValue({ default_key_id: 7, default_key_state: 'active', token: 'new-default' })
    await vm.keyAction({ id: 7, kind: 'default_human', assigned_type: 'human' }, 'rotate')
    expect(shown(vm)).toBeUndefined()
    expect(vm.credentialIssue.status).toBe('unavailable')
    expect(vm.myToken).not.toBe('old-default')
  })

  test.each(['default_human', 'additional_human'])('%s rotation still reveals a verified current key', async kind => {
    const vm = dashboard()
    vm.api.mockResolvedValueOnce({ id: 8, secret: 'current-human-key' })
      .mockResolvedValue(kind === 'default_human'
        ? { default_key_id: 8, default_key_state: 'active', token: 'current-human-key' }
        : [{ id: 8, state: 'active' }])
    await vm.keyAction({ id: 7, kind, assigned_type: 'human' }, 'rotate')
    expect(shown(vm)).toBe('current-human-key')
    if (kind === 'default_human') {
      expect(vm.myToken).toBe('current-human-key')
      expect(vm._myTokenOrg).toBe('team')
    }
    vm.dismissCredentialIssue()
    expect(vm.newApiKey).toBeNull()
    expect(vm.credentialIssues).toEqual({})
    expect(JSON.stringify(vi.mocked(storageSet).mock.calls)).not.toContain('current-human-key')
  })
})
