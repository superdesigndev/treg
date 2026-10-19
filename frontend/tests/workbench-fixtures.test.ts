import { readFileSync } from 'node:fs'
import { Readable } from 'node:stream'
import { expect, test } from 'vitest'
import { workbenchFixtures } from '../scripts/fixture-plugin.mjs'

const fixture = (name: string) => JSON.parse(readFileSync(new URL(`../../contracts/workbench/fixtures/${name}.json`, import.meta.url), 'utf8'))

function request(method: string, path: string, body: string | Buffer[] = '', headers: Record<string, string> = {}) {
  let middleware: (...args: any[]) => unknown
  workbenchFixtures().configureServer({ middlewares: { use(fn: typeof middleware) { middleware = fn } } })
  const req = Object.assign(Readable.from(Array.isArray(body) ? body : [Buffer.from(body)]), {
    method, url: path, headers,
  })
  return new Promise<{ status: number, body: any, headers: Record<string, string> }>((resolve, reject) => {
    const responseHeaders: Record<string, string> = {}
    const res = {
      statusCode: 200,
      setHeader(name: string, value: string) { responseHeaders[name] = value },
      end(value: string | Buffer) {
        resolve({ status: res.statusCode, body: JSON.parse(value.toString()), headers: responseHeaders })
      },
    }
    Promise.resolve(middleware!(req, res, (error?: unknown) => error ? reject(error) : resolve({ status: 0, body: null, headers: {} }))).catch(reject)
  })
}

test.each([
  ['POST', '/query', 'query.default', 200],
  ['POST', '/tables/tbl_01HZX1LEADS/rows', 'writeRows.default', 200],
  ['POST', '/tables/tbl_01HZX1LEADS/rows/delete', 'deleteRows.default', 200],
  ['POST', '/tables/tbl_01HZX1LEADS/export', 'exportTable.default', 202],
  ['POST', '/tables/tbl_01HZX1LEADS/schema-changes', 'createSchemaChange.default', 202],
  ['GET', '/table-operations/top_create_001', 'getTableOperation.default', 200],
  ['POST', '/runs/r_8f21/stop', 'stopRun.default', 202],
  ['POST', '/runs/r_8e90/retry-failed', 'retryFailedRows.default', 202],
  ['POST', '/tables', 'createTable.default', 202],
])('returns the contract status and serialized fixture for %s %s', async (method, path, name, status) => {
  const response = await request(method, `/api/workbench${path}`, '{}')
  expect(response.status).toBe(status)
  expect(response.body).toEqual(fixture(name))
  expect(response.headers['content-type']).toBe('application/json')
})

test('a dry run returns its preview, while execution returns an accepted Run handle', async () => {
  const path = '/api/workbench/installs/lead-emails/run'
  const preview = await request('POST', path, '{"dry_run":true}')
  expect(preview.status).toBe(200)
  expect(preview.body).toEqual(fixture('runInstall.dryRun'))
  for (const wait_s of [0, 60]) {
    const accepted = await request('POST', path, JSON.stringify({ dry_run: false, wait_s }))
    expect(accepted.status).toBe(202)
    expect(accepted.body).toEqual(fixture('runInstall.handle'))
  }
})

test.each([
  ['/tables', 'ready', 'createTable.ready', 201],
  ['/tables', 'replay', 'createTable.replay', 200],
  ['/runs/r_8e90/stop', 'terminal', 'stopRun.terminal', 200],
  ['/installs/q4-accounts.people/run', 'terminal', 'runInstall.terminal', 200],
])('selects an explicit dev-only %s %s case with its own status', async (path, selected, name, status) => {
  const response = await request('POST', `/api/workbench${path}`, '{}', { 'x-treg-fixture-case': selected })
  expect(response.status).toBe(status)
  expect(response.body).toEqual(fixture(name))
})

test('schema preflight returns a preview instead of accepting a maintenance operation', async () => {
  const response = await request('POST', '/api/workbench/tables/tbl_01HZX1LEADS/schema-changes', '{"dry_run":true}')
  expect(response.status).toBe(200)
  expect(response.body).toEqual(fixture('createSchemaChange.preflight'))
  expect(response.body.operation_id).toBeUndefined()
})

test('failure pages preserve their selection identity and blocked retry item', async () => {
  const path = '/api/workbench/runs/r_8e90/failures'
  const first = await request('GET', `${path}?limit=2`)
  const second = await request('GET', `${path}?cursor=${encodeURIComponent(first.body.next_cursor)}`)
  expect(first.status).toBe(200)
  expect(second.status).toBe(200)
  expect(second.body.selection_id).toBe(first.body.selection_id)
  expect([...first.body.items, ...second.body.items]).toEqual(fixture('listRunFailures.default').items)
  expect(second.body.items.some((item: { retry: { eligible: boolean } }) => !item.retry.eligible)).toBe(true)
  expect(second.body.next_cursor).toBeNull()
})

test('rows paginate without repeating records or cursors and eventually end', async () => {
  const ids: string[] = []
  const cursors = new Set<string>()
  let cursor: string | null = null
  do {
    const response = await request('GET', `/api/workbench/tables/tbl_01HZX1LEADS/rows?limit=2${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`)
    expect(response.status).toBe(200)
    expect(response.body.rows.length).toBeLessThanOrEqual(2)
    ids.push(...response.body.rows.map((row: { id: string }) => row.id))
    cursor = response.body.next_cursor
    if (cursor) {
      expect(cursors.has(cursor)).toBe(false)
      cursors.add(cursor)
    }
    expect(cursors.size).toBeLessThan(50)
  } while (cursor)
  expect(ids).toEqual(fixture('listRows.leads').rows.map((row: { id: string }) => row.id))
  expect(new Set(ids).size).toBe(ids.length)
})

test('list and trace cursors progress, reject wrong resources, and stop at the fixture boundary', async () => {
  for (const [path, field, cursorParam, nextField] of [
    ['/runs', 'runs', 'cursor', 'next_cursor'],
    ['/runs/r_8e90', 'trace', 'trace_cursor', 'trace_next_cursor'],
  ]) {
    const first = await request('GET', `/api/workbench${path}?limit=2`)
    expect(first.status).toBe(200)
    expect(first.body[field]).toHaveLength(2)
    const cursor = first.body[nextField]
    expect(cursor).toBeTruthy()
    const second = await request('GET', `/api/workbench${path}?limit=200&${cursorParam}=${encodeURIComponent(cursor)}`)
    expect(second.status).toBe(200)
    expect(second.body[field]).not.toEqual(first.body[field])
    expect(second.body[nextField]).toBeNull()
    const wrongResource = await request('GET', `/api/workbench/tables?cursor=${encodeURIComponent(cursor)}`)
    expect(wrongResource.status).toBe(400)
    expect(wrongResource.body.code).toBe('invalid_cursor')
  }
})

test.each(['bad%', '', 'e30'])('rejects an invalid cursor: %s', async cursor => {
  const response = await request('GET', `/api/workbench/tables/tbl_01HZX1LEADS/rows?cursor=${encodeURIComponent(cursor)}`)
  expect(response.status).toBe(400)
  expect(response.body.code).toBe('invalid_cursor')
})

test.each([
  ['/tables/tbl_01HZX1LEADS/rows', 1000], ['/runs', 200], ['/runs/r_8e90', 200],
])('uses the contract page-size bound for %s', async (path, maximum) => {
  expect((await request('GET', `/api/workbench${path}?limit=${maximum}`)).status).toBe(200)
  const response = await request('GET', `/api/workbench${path}?limit=${maximum + 1}`)
  expect(response.status).toBe(400)
  expect(response.body.code).toBe('invalid_request')
})

test('cursor binds filters while allowing reordered parameters and a different page size', async () => {
  const first = await request('GET', '/api/workbench/runs?install=lead-emails&status=running&limit=2')
  const cursor = encodeURIComponent(first.body.next_cursor)
  const sameFilters = await request('GET', `/api/workbench/runs?status=running&install=lead-emails&limit=200&cursor=${cursor}`)
  expect(sameFilters.status).toBe(200)
  expect(sameFilters.body.runs).not.toEqual(first.body.runs)
  for (const query of ['install=lead-emails&status=queued', 'status=running', '']) {
    const response = await request('GET', `/api/workbench/runs?${query}&cursor=${cursor}`)
    expect(response.status).toBe(400)
    expect(response.body.code).toBe('invalid_cursor')
  }
})

test.each(['../getTable.leads', 'constructor', 'toString'])('only permits explicit dev fixture cases: %s', async selected => {
  const response = await request('POST', '/api/workbench/installs/lead-emails/run', '{}', {
    'x-treg-fixture-case': selected,
  })
  expect(response.status).toBe(400)
  expect(response.body.code).toBe('invalid_request')
})

test.each(['{', 'null', '[]'])('rejects malformed or non-object request JSON: %s', async body => {
  const response = await request('POST', '/api/workbench/installs/lead-emails/run', body)
  expect(response.status).toBe(400)
  expect(response.body.code).toBe('invalid_request')
})

test('bounds streamed request bodies without trusting Content-Length', async () => {
  const response = await request('POST', '/api/workbench/installs/lead-emails/run', [
    Buffer.alloc(40_000, ' '), Buffer.alloc(40_000, ' '),
  ])
  expect(response.status).toBe(413)
  expect(response.body.code).toBe('request_too_large')
})

test('unmatched paths pass through and an unknown Workbench route is JSON', async () => {
  expect((await request('GET', '/catalog')).status).toBe(0)
  const response = await request('GET', '/api/workbench/tables/tbl_01HZX1LEADS/rows/unrecognized')
  expect(response.status).toBe(404)
  expect(response.body.code).toBe('not_found')
  expect(response.headers['content-type']).toBe('application/json')
})
