import type { components, paths } from './types.gen'

export type Schemas = components['schemas']
export type TableSummary = Schemas['TableSummary']
export type Table = Schemas['Table']
export type Row = Schemas['Row']
export type Run = Schemas['Run']
export type RunDetail = Schemas['RunDetail']
export type Install = Schemas['Install']
export type InstallDetail = Schemas['InstallDetail']
export type Trigger = Schemas['Trigger']
export type ToolDefinition = Schemas['ToolDefinition']
export type UnattendedBudget = Schemas['UnattendedBudget']
export type WorkbenchError = Schemas['Error']

type Json<T> = T extends { content: { 'application/json': infer B } } ? B : never
export type SuccessBody<R> = {
  [S in keyof R]: S extends number | string
    ? `${S}` extends `2${string}` ? Json<R[S]> : never
    : never
}[keyof R]
type Ok<P extends keyof paths, M extends keyof paths[P]> =
  paths[P][M] extends { responses: infer R } ? SuccessBody<R> : never
type Query = Json<paths['/query']['post']['requestBody']>

export type ApiCall = (path: string, opts?: { method?: string, headers?: Record<string, string>, body?: string }) => Promise<unknown>

const BASE = '/api/workbench'
const q = (params: Record<string, string | number | undefined>) => {
  const s = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') s.set(k, String(v))
  const out = s.toString()
  return out ? `?${out}` : ''
}
const json = (body: unknown) => ({ headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })

export function workbenchClient(api: ApiCall) {
  const get = <T>(path: string) => api(BASE + path) as Promise<T>
  const send = <T>(method: string, path: string, body?: unknown, headers?: Record<string, string>) =>
    api(BASE + path, { method, ...(body === undefined ? {} : json(body)), ...(headers ? { headers: { ...(body === undefined ? {} : json(body).headers), ...headers } } : {}) }) as Promise<T>
  return {
    listTables: (cursor?: string, limit?: number) => get<Ok<'/tables', 'get'>>(`/tables${q({ cursor, limit })}`),
    createTable: (body: Schemas['TableCreate'], idempotencyKey: string) =>
      send<Ok<'/tables', 'post'>>('POST', '/tables', body, { 'Idempotency-Key': idempotencyKey }),
    getTable: (id: string) => get<Ok<'/tables/{tableId}', 'get'>>(`/tables/${encodeURIComponent(id)}`),
    updateTable: (id: string, body: Schemas['TableUpdate'], idempotencyKey: string) =>
      send<Ok<'/tables/{tableId}', 'patch'>>('PATCH', `/tables/${encodeURIComponent(id)}`, body, { 'Idempotency-Key': idempotencyKey }),
    listRows: (id: string, cursor?: string, limit?: number) => get<Ok<'/tables/{tableId}/rows', 'get'>>(`/tables/${encodeURIComponent(id)}/rows${q({ cursor, limit })}`),
    writeRows: (id: string, body: Schemas['RowsWrite'], idempotencyKey: string) =>
      send<Ok<'/tables/{tableId}/rows', 'post'>>('POST', `/tables/${encodeURIComponent(id)}/rows`, body, { 'Idempotency-Key': idempotencyKey }),
    deleteRows: (id: string, body: Schemas['RowsDelete'], idempotencyKey: string) =>
      send<Ok<'/tables/{tableId}/rows/delete', 'post'>>('POST', `/tables/${encodeURIComponent(id)}/rows/delete`, body, { 'Idempotency-Key': idempotencyKey }),
    createSchemaChange: (id: string, body: Schemas['SchemaChangeRequest'], idempotencyKey: string) =>
      send<Ok<'/tables/{tableId}/schema-changes', 'post'>>('POST', `/tables/${encodeURIComponent(id)}/schema-changes`, body, { 'Idempotency-Key': idempotencyKey }),
    getTableOperation: (id: string) => get<Ok<'/table-operations/{operationId}', 'get'>>(`/table-operations/${encodeURIComponent(id)}`),
    query: (sql: string, limit?: number, options: Pick<Query, 'params' | 'database_generation'> = {}) =>
      send<Ok<'/query', 'post'>>('POST', '/query', { sql, limit, ...options }),
    listRuns: (filter: { install?: string, table?: string, status?: Schemas['RunStatus'], cursor?: string, limit?: number } = {}) => get<Ok<'/runs', 'get'>>(`/runs${q(filter)}`),
    getRun: (id: string, traceCursor?: string, limit?: number) => get<Ok<'/runs/{runId}', 'get'>>(`/runs/${encodeURIComponent(id)}${q({ trace_cursor: traceCursor, limit })}`),
    stopRun: (id: string) => send<Ok<'/runs/{runId}/stop', 'post'>>('POST', `/runs/${encodeURIComponent(id)}/stop`),
    listRunFailures: (id: string, cursor?: string, limit?: number) =>
      get<Ok<'/runs/{runId}/failures', 'get'>>(`/runs/${encodeURIComponent(id)}/failures${q({ cursor, limit })}`),
    retryFailedRows: (id: string, body: Schemas['RetryRequest'], idempotencyKey: string) =>
      send<Ok<'/runs/{runId}/retry-failed', 'post'>>('POST', `/runs/${encodeURIComponent(id)}/retry-failed`, body, { 'Idempotency-Key': idempotencyKey }),
    listInstalls: (cursor?: string, limit?: number) => get<Ok<'/installs', 'get'>>(`/installs${q({ cursor, limit })}`),
    getInstall: (name: string) => get<Ok<'/installs/{installName}', 'get'>>(`/installs/${encodeURIComponent(name)}`),
    saveInstall: (name: string, body: Schemas['InstallSave'], idempotencyKey: string) =>
      send<Ok<'/installs/{installName}', 'put'>>('PUT', `/installs/${encodeURIComponent(name)}`, body, { 'Idempotency-Key': idempotencyKey }),
    runInstall: (name: string, body: Schemas['RunRequest'], idempotencyKey: string) =>
      send<Ok<'/installs/{installName}/run', 'post'>>('POST', `/installs/${encodeURIComponent(name)}/run`, body, { 'Idempotency-Key': idempotencyKey }),
    listTriggers: (cursor?: string, limit?: number) => get<Ok<'/triggers', 'get'>>(`/triggers${q({ cursor, limit })}`),
    listMyTools: (cursor?: string, limit?: number) => get<Ok<'/tools/mine', 'get'>>(`/tools/mine${q({ cursor, limit })}`),
    getTool: (id: string) => get<Ok<'/tools/{toolId}', 'get'>>(`/tools/${encodeURIComponent(id)}`),
  }
}

export type WorkbenchClient = ReturnType<typeof workbenchClient>
