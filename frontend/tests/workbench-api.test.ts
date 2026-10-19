import { readFileSync } from 'node:fs'
import { afterEach, expect, expectTypeOf, test, vi } from 'vitest'
import { ApiError, requestJson } from '../src/api'
import { workbenchClient, type ApiCall, type Schemas, type SuccessBody } from '../src/workbench/api/client'

afterEach(() => vi.unstubAllGlobals())

const fixture = (name: string) => JSON.parse(readFileSync(new URL(`../../contracts/workbench/fixtures/${name}.json`, import.meta.url), 'utf8'))
const transport: ApiCall = (path, opts = {}) => requestJson(path, opts, {}, () => {}, () => false)

test('creation, metadata edits and maintenance preserve caller identity and payload', async () => {
  const accepted = { operation: { operation_id: 'op_create', status: 'pending' } }
  const api = vi.fn<ApiCall>().mockResolvedValue(accepted)
  const client = workbenchClient(api)
  const create: Schemas['TableCreate'] = {
    name: 'leads', kind: 'people', columns: [{ sql_name: 'email', label: 'Email', storage_type: 'TEXT', nullable: true }],
  }
  expect(await client.createTable(create, 'create-fixed-key')).toBe(accepted)
  expect(api).toHaveBeenLastCalledWith('/api/workbench/tables', {
    method: 'POST', headers: { 'content-type': 'application/json', 'Idempotency-Key': 'create-fixed-key' },
    body: JSON.stringify(create),
  })

  const update: Schemas['TableUpdate'] = {
    expected_metadata_revision: 4, column_updates: [{ column_id: 'col_email', label: 'Work email' }],
  }
  await client.updateTable('tbl/one', update, 'update-fixed-key')
  expect(api).toHaveBeenLastCalledWith('/api/workbench/tables/tbl%2Fone', {
    method: 'PATCH', headers: { 'content-type': 'application/json', 'Idempotency-Key': 'update-fixed-key' },
    body: JSON.stringify(update),
  })

  const change: Schemas['SchemaChangeRequest'] = {
    database_generation: 2, expected_schema_version: 3, expected_metadata_revision: 4,
    dry_run: false, changes: [{ op: 'rename_column', column_id: 'col_email', sql_name: 'work_email' }],
  }
  await client.createSchemaChange('tbl/one', change, 'schema-fixed-key')
  expect(api).toHaveBeenLastCalledWith('/api/workbench/tables/tbl%2Fone/schema-changes', {
    method: 'POST', headers: { 'content-type': 'application/json', 'Idempotency-Key': 'schema-fixed-key' },
    body: JSON.stringify(change),
  })
  await client.getTableOperation('op/create')
  expect(api).toHaveBeenLastCalledWith('/api/workbench/table-operations/op%2Fcreate')
})

test('a write refusal is not retried and an explicit retry keeps the complete frozen request', async () => {
  const unknown = new Error('write_outcome_unknown')
  const receipt = { operation_id: 'op_write', committed: true }
  const api = vi.fn<ApiCall>().mockRejectedValueOnce(unknown).mockResolvedValueOnce(receipt)
  const client = workbenchClient(api)
  const write: Schemas['RowsWrite'] = {
    database_generation: 2, expected_schema_version: 3, mode: 'patch',
    rows: [{ id: 'row_1', expected_revision: 7, values: { email: null } }],
  }
  const original = JSON.stringify(write)
  await expect(client.writeRows('tbl/one', write, 'wk1.frozen-write')).rejects.toBe(unknown)
  expect(api).toHaveBeenCalledTimes(1)
  expect(await client.writeRows('tbl/one', write, 'wk1.frozen-write')).toBe(receipt)
  expect(api).toHaveBeenCalledTimes(2)
  expect(api.mock.calls[0]).toEqual(api.mock.calls[1])
  expect(api).toHaveBeenLastCalledWith('/api/workbench/tables/tbl%2Fone/rows', {
    method: 'POST', headers: { 'content-type': 'application/json', 'Idempotency-Key': 'wk1.frozen-write' },
    body: original,
  })
  expect(JSON.stringify(write)).toBe(original)
})

test('row deletion passes every revision and its explicit idempotency key', async () => {
  const api = vi.fn<ApiCall>().mockResolvedValue({ committed: true })
  const request: Schemas['RowsDelete'] = {
    database_generation: 2, expected_schema_version: 3, rows: [{ id: 'row_1', expected_revision: 7 }],
  }
  await workbenchClient(api).deleteRows('tbl/one', request, 'wk1.frozen-delete')
  expect(api).toHaveBeenCalledExactlyOnceWith('/api/workbench/tables/tbl%2Fone/rows/delete', {
    method: 'POST', headers: { 'content-type': 'application/json', 'Idempotency-Key': 'wk1.frozen-delete' },
    body: JSON.stringify(request),
  })
})

test('queries bind values and generation while pagination preserves opaque cursors', async () => {
  const api = vi.fn<ApiCall>().mockResolvedValue({})
  const client = workbenchClient(api)
  await client.query('SELECT * FROM leads WHERE email = :email', 20, {
    params: { email: "a'); DROP TABLE leads; --" }, database_generation: 2,
  })
  expect(api).toHaveBeenLastCalledWith('/api/workbench/query', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      sql: 'SELECT * FROM leads WHERE email = :email', limit: 20,
      params: { email: "a'); DROP TABLE leads; --" }, database_generation: 2,
    }),
  })
  await client.query('SELECT 1', 1)
  expect(JSON.parse(api.mock.calls.at(-1)![1]!.body!)).toEqual({ sql: 'SELECT 1', limit: 1 })
  await client.listTables('page/+ next', 20)
  expect(api).toHaveBeenLastCalledWith('/api/workbench/tables?cursor=page%2F%2B+next&limit=20')
  await client.listRows('tbl/one', 'page/+ next', 10)
  expect(api).toHaveBeenLastCalledWith('/api/workbench/tables/tbl%2Fone/rows?cursor=page%2F%2B+next&limit=10')
})

test('success types retain completed, created and accepted response branches', () => {
  type Responses = {
    200: { content: { 'application/json': { kind: 'completed' } } }
    201: { content: { 'application/json': { kind: 'created' } } }
    202: { content: { 'application/json': { kind: 'accepted' } } }
    409: { content: { 'application/json': { kind: 'conflict' } } }
  }
  expectTypeOf<SuccessBody<Responses>>().toEqualTypeOf<
    { kind: 'completed' } | { kind: 'created' } | { kind: 'accepted' }
  >()
  type SchemaResponse = Awaited<ReturnType<ReturnType<typeof workbenchClient>['createSchemaChange']>>
  expectTypeOf<SchemaResponse>().toEqualTypeOf<Schemas['SchemaPreflight'] | Schemas['TableOperation']>()
  type RunResponse = Awaited<ReturnType<ReturnType<typeof workbenchClient>['runInstall']>>
  expectTypeOf<RunResponse>().toEqualTypeOf<Schemas['RunDetail'] | Schemas['RunHandle'] | Schemas['DryRun']>()
  type RetryResponse = Awaited<ReturnType<ReturnType<typeof workbenchClient>['retryFailedRows']>>
  expectTypeOf<RetryResponse>().toEqualTypeOf<Schemas['RunDetail'] | Schemas['RunHandle']>()
})

test.each([
  [200, 'getRun.r_8e90.partial', false],
  [202, 'runInstall.handle', false],
  [200, 'runInstall.dryRun', true],
] as const)('run admission preserves the %i %s response branch', async (status, name, dryRun) => {
  const response = fixture(name)
  const fetcher = vi.fn().mockResolvedValue(Response.json(response, { status }))
  vi.stubGlobal('fetch', fetcher)
  const request: Schemas['RunRequest'] = {
    install_id: 'ins_lead', expected_install_revision: 4, dry_run: dryRun, wait_s: 0,
    input: { country: 'US' }, max_cost_micro: 5000000,
  }
  expect(await workbenchClient(transport).runInstall('lead/emails', request, 'run-fixed-key')).toEqual(response)
  expect(fetcher).toHaveBeenCalledExactlyOnceWith('/api/workbench/installs/lead%2Femails/run', {
    credentials: 'include', method: 'POST',
    headers: { 'content-type': 'application/json', 'Idempotency-Key': 'run-fixed-key' },
    body: JSON.stringify(request),
  })
})

test('an explicit retry of admission preserves the key, selected items and execution options', async () => {
  const fetcher = vi.fn()
    .mockRejectedValueOnce(new Error('connection lost'))
    .mockResolvedValueOnce(Response.json(fixture('retryFailedRows.default'), { status: 202 }))
  vi.stubGlobal('fetch', fetcher)
  const client = workbenchClient(transport)
  const retry: Schemas['RetryRequest'] = {
    selection: { type: 'items', selection_id: 'sel_1', item_ids: ['item_2', 'item_7'] },
    input_mode: 'current_rows', options: { expected_install_revision: 4, max_cost_micro: 1000000 }, wait_s: 0,
  }
  await expect(client.retryFailedRows('run/one', retry, 'retry-fixed-key')).rejects.toThrow('connection lost')
  expect(fetcher).toHaveBeenCalledTimes(1)
  await client.retryFailedRows('run/one', retry, 'retry-fixed-key')
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(fetcher.mock.calls[0]).toEqual(fetcher.mock.calls[1])
  expect(fetcher).toHaveBeenLastCalledWith('/api/workbench/runs/run%2Fone/retry-failed', {
    credentials: 'include', method: 'POST',
    headers: { 'content-type': 'application/json', 'Idempotency-Key': 'retry-fixed-key' },
    body: JSON.stringify(retry),
  })
})

test('optional execution defaults remain server-owned in both types and requests', async () => {
  const api = vi.fn<ApiCall>().mockResolvedValue({})
  const client = workbenchClient(api)
  const run: Schemas['RunRequest'] = { install_id: 'ins_lead', expected_install_revision: 4 }
  await client.runInstall('lead-emails', run, 'run-default-key')
  expect(JSON.parse(api.mock.calls.at(-1)![1]!.body!)).toEqual(run)
  const retry: Schemas['RetryRequest'] = {
    selection: { type: 'failed', selection_id: 'sel_1' }, input_mode: 'current_rows',
  }
  await client.retryFailedRows('run_1', retry, 'retry-default-key')
  expect(JSON.parse(api.mock.calls.at(-1)![1]!.body!)).toEqual(retry)
})

test('rejected selections retain the transport error and its recovery details', async () => {
  const body = {
    code: 'revision_stale', detail: 'The Install changed.',
    recovery: { config_revision: 5, selection_id: 'sel_1' },
  }
  const fetcher = vi.fn().mockResolvedValue(Response.json(body, { status: 409 }))
  vi.stubGlobal('fetch', fetcher)
  const attempt = workbenchClient(transport).retryFailedRows('run_1', {
    selection: { type: 'failed', selection_id: 'sel_1' }, input_mode: 'current_rows', wait_s: 0,
  }, 'retry-fixed-key')
  await expect(attempt).rejects.toBeInstanceOf(ApiError)
  await expect(attempt).rejects.toMatchObject({ status: 409, detail: body.detail, body })
  expect(fetcher).toHaveBeenCalledTimes(1)
})

test('saving an Install preserves stable identities, revisions and the supplied trigger list', async () => {
  const api = vi.fn<ApiCall>().mockResolvedValue({})
  const save: Schemas['InstallSave'] = {
    install_id: 'ins_lead', config_revision: 4, paused: true,
    triggers: [{ id: 'trg_weekly', key: 'weekly', kind: 'schedule', config_revision: 2, paused: true }],
  }
  const original = JSON.stringify(save)
  await workbenchClient(api).saveInstall('lead/emails', save, 'save-fixed-key')
  expect(api).toHaveBeenCalledExactlyOnceWith('/api/workbench/installs/lead%2Femails', {
    method: 'PUT', headers: { 'content-type': 'application/json', 'Idempotency-Key': 'save-fixed-key' },
    body: original,
  })
  expect(JSON.stringify(save)).toBe(original)
})

test('Run failure and management lists pass bounded cursor pagination, and stop adds no identity', async () => {
  const api = vi.fn<ApiCall>().mockResolvedValue({})
  const client = workbenchClient(api)
  await client.getRun('run/one', 'cursor/+ one', 25)
  expect(api).toHaveBeenLastCalledWith('/api/workbench/runs/run%2Fone?trace_cursor=cursor%2F%2B+one&limit=25')
  await client.listRunFailures('run/one', 'cursor/+ one', 25)
  expect(api).toHaveBeenLastCalledWith('/api/workbench/runs/run%2Fone/failures?cursor=cursor%2F%2B+one&limit=25')
  await client.listRuns({ install: 'lead emails', status: 'partial', cursor: 'cursor_1', limit: 25 })
  expect(api).toHaveBeenLastCalledWith('/api/workbench/runs?install=lead+emails&status=partial&cursor=cursor_1&limit=25')
  for (const [method, path] of [
    [client.listInstalls, '/installs'], [client.listTriggers, '/triggers'], [client.listMyTools, '/tools/mine'],
  ] as const) {
    await method('cursor_1', 25)
    expect(api).toHaveBeenLastCalledWith(`/api/workbench${path}?cursor=cursor_1&limit=25`)
  }
  await client.stopRun('run/one')
  expect(api).toHaveBeenLastCalledWith('/api/workbench/runs/run%2Fone/stop', { method: 'POST' })
})

test('generated request types require row and configuration concurrency preconditions', () => {
  // @ts-expect-error A patch row must carry its expected revision.
  const missingRowRevision: Schemas['RowsWrite'] = { database_generation: 1, expected_schema_version: 1, mode: 'patch', rows: [{ id: 'row_1', values: {} }] }
  // @ts-expect-error A patch row must carry its stable row identity.
  const missingRowId: Schemas['RowsWrite'] = { database_generation: 1, expected_schema_version: 1, mode: 'patch', rows: [{ expected_revision: 1, values: {} }] }
  // @ts-expect-error Updating an Install requires its stable identity even when a tool is supplied.
  const missingInstallId: Schemas['InstallSave'] = { config_revision: 4, tool: 'team.leads@1', paused: true }
  // @ts-expect-error Updating a Trigger requires its stable identity.
  const missingTriggerId: Schemas['TriggerSave'] = { config_revision: 2, key: 'weekly', kind: 'schedule', paused: true }
  // @ts-expect-error Creating an Install requires a Tool version.
  const missingCreationTool: Schemas['InstallSave'] = { config_revision: 0 }
  // @ts-expect-error Creating a Trigger requires its execution identity.
  const missingTriggerMembership: Schemas['TriggerSave'] = { config_revision: 0, key: 'weekly', kind: 'schedule' }
  // @ts-expect-error Upsert requires an explicit conflict policy.
  const missingConflictPolicy: Schemas['RowsWrite'] = { database_generation: 1, expected_schema_version: 1, mode: 'upsert', rows: [{ values: {} }] }
  void [missingRowRevision, missingRowId, missingInstallId, missingTriggerId, missingCreationTool, missingTriggerMembership, missingConflictPolicy]

  const createInstall: Schemas['InstallSave'] = { config_revision: 0, tool: 'team.leads@1' }
  const updateInstall: Schemas['InstallSave'] = { config_revision: 4, install_id: 'ins_lead', paused: true }
  const createTrigger: Schemas['TriggerSave'] = { config_revision: 0, key: 'weekly', kind: 'schedule', run_as_membership_id: 1 }
  const updateTrigger: Schemas['TriggerSave'] = { config_revision: 2, id: 'trg_weekly', key: 'weekly', kind: 'schedule' }
  const patch: Schemas['RowsWrite'] = {
    database_generation: 1, expected_schema_version: 1, mode: 'patch',
    rows: [{ id: 'row_1', expected_revision: 1, values: { email: null } }],
  }
  expectTypeOf(createInstall).toExtend<Schemas['InstallSave']>()
  expectTypeOf(updateInstall).toExtend<Schemas['InstallSave']>()
  expectTypeOf(createTrigger).toExtend<Schemas['TriggerSave']>()
  expectTypeOf(updateTrigger).toExtend<Schemas['TriggerSave']>()
  expectTypeOf(patch).toExtend<Schemas['RowsWrite']>()
})
