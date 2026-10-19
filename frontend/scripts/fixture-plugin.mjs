import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const FIXTURES = fileURLToPath(new URL('../../contracts/workbench/fixtures/', import.meta.url))
const MAX_BODY_BYTES = 64 * 1024

// Only mounted when TREG_FIXTURES=1: the dev proxy otherwise forwards /api/workbench to the Python
// server, and the same requests must keep reaching it once the real routes exist.
const ROUTES = [
  ['GET', /^\/tables$/, 'listTables.default', 200, 'tables'],
  ['POST', /^\/tables$/, 'createTable.default', 202],
  ['GET', /^\/tables\/tbl_01HZX1NEW$/, 'getTable.creating', 200],
  ['GET', /^\/tables\/[^/]+$/, 'getTable.leads', 200],
  ['PATCH', /^\/tables\/[^/]+$/, 'getTable.leads', 200],
  ['GET', /^\/tables\/[^/]+\/rows$/, 'listRows.leads', 200, 'rows'],
  ['POST', /^\/tables\/[^/]+\/rows$/, 'writeRows.default', 200],
  ['POST', /^\/tables\/[^/]+\/rows\/delete$/, 'deleteRows.default', 200],
  ['POST', /^\/tables\/[^/]+\/export$/, 'exportTable.default', 202],
  ['POST', /^\/tables\/[^/]+\/schema-changes$/, 'createSchemaChange.default', 202],
  ['GET', /^\/table-operations\/[^/]+$/, 'getTableOperation.default', 200],
  ['POST', /^\/query$/, 'query.default', 200],
  ['GET', /^\/runs$/, 'listRuns.default', 200, 'runs'],
  ['GET', /^\/runs\/[^/]+\/failures$/, 'listRunFailures.default', 200, 'items'],
  ['GET', /^\/runs\/r_8e90$/, 'getRun.r_8e90.partial', 200, 'trace'],
  ['GET', /^\/runs\/[^/]+$/, 'getRun.r_8f21', 200, 'trace'],
  ['POST', /^\/runs\/[^/]+\/stop$/, 'stopRun.default', 202],
  ['POST', /^\/runs\/[^/]+\/retry-failed$/, 'retryFailedRows.default', 202],
  ['GET', /^\/installs$/, 'listInstalls.default', 200, 'installs'],
  ['GET', /^\/installs\/competitor-watch$/, 'getInstall.competitor-watch', 200],
  ['GET', /^\/installs\/[^/]+$/, 'getInstall.lead-emails', 200],
  ['PUT', /^\/installs\/[^/]+$/, 'getInstall.lead-emails', 200],
  ['POST', /^\/installs\/[^/]+\/run$/, 'runInstall.handle', 202],
  ['GET', /^\/tools\/mine$/, 'listMyTools.default', 200, 'tools'],
  ['GET', /^\/tools\/[^/]+$/, 'getTool.lead-emails', 200],
  ['GET', /^\/triggers$/, 'listTriggers.default', 200, 'triggers'],
]

const CASES = {
  'createTable.default': { ready: ['createTable.ready', 201], replay: ['createTable.replay', 200] },
  'createSchemaChange.default': { preflight: ['createSchemaChange.preflight', 200] },
  'stopRun.default': { terminal: ['stopRun.terminal', 200] },
  'runInstall.handle': { terminal: ['runInstall.terminal', 200] },
}

class FixtureRequestError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let chunks = [], size = 0, tooLarge = false
    req.on('data', chunk => {
      if (tooLarge) return
      const bytes = Buffer.from(chunk)
      size += bytes.length
      if (size > MAX_BODY_BYTES) {
        tooLarge = true
        chunks = []
        reject(new FixtureRequestError(413, 'request_too_large', 'Fixture request exceeds 64 KiB'))
      } else chunks.push(bytes)
    })
    req.on('error', () => reject(new FixtureRequestError(400, 'invalid_request', 'Request body could not be read')))
    req.on('aborted', () => reject(new FixtureRequestError(400, 'invalid_request', 'Request body was interrupted')))
    req.on('end', () => {
      if (tooLarge) return
      try {
        const body = size ? JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks))) : {}
        if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error()
        resolve(body)
      } catch {
        reject(new FixtureRequestError(400, 'invalid_request', 'Expected a JSON object'))
      }
    })
  })
}

function paginate(payload, url, field) {
  if (!field) return payload
  const cursorName = field === 'trace' ? 'trace_cursor' : 'cursor'
  const nextName = field === 'trace' ? 'trace_next_cursor' : 'next_cursor'
  const defaultLimit = field === 'rows' ? 200 : 50
  const maximum = field === 'rows' ? 1000 : 200
  const limit = Number(url.searchParams.get('limit') ?? defaultLimit)
  if (!Number.isInteger(limit) || limit < 1 || limit > maximum) {
    throw new FixtureRequestError(400, 'invalid_request', `Fixture page limit must be between 1 and ${maximum}`)
  }
  const filters = new URLSearchParams(url.searchParams)
  filters.delete(cursorName)
  filters.delete('limit')
  filters.sort()
  const query = filters.toString()
  let offset = 0
  const cursor = url.searchParams.get(cursorName)
  if (cursor !== null) {
    try {
      if (!/^[A-Za-z0-9_-]+$/.test(cursor)) throw new Error()
      const decoded = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8'))
      if (decoded.path !== url.pathname || decoded.query !== query || !Number.isInteger(decoded.offset) || decoded.offset < 1 || decoded.offset > payload[field].length) throw new Error()
      offset = decoded.offset
    } catch {
      throw new FixtureRequestError(400, 'invalid_cursor', 'Cursor does not identify a fixture page for this resource')
    }
  }
  const end = Math.min(offset + limit, payload[field].length)
  const next = end < payload[field].length
    ? Buffer.from(JSON.stringify({ path: url.pathname, query, offset: end })).toString('base64url') : null
  return { ...payload, [field]: payload[field].slice(offset, end), [nextName]: next,
    ...(field === 'trace' ? {} : { has_more: next !== null }) }
}

function respond(res, status, payload) {
  res.setHeader('content-type', 'application/json')
  res.statusCode = status
  res.end(JSON.stringify(payload))
}

export function workbenchFixtures() {
  return {
    name: 'treg-workbench-fixtures',
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        try {
          const url = new URL(req.url, 'http://localhost')
          if (!url.pathname.startsWith('/api/workbench/')) return next()
          const path = url.pathname.slice('/api/workbench'.length)
          const hit = ROUTES.find(([method, re]) => method === req.method && re.test(path))
          if (!hit) return respond(res, 404, { detail: `no fixture for ${req.method} ${path}`, code: 'not_found' })
          const body = req.method === 'GET' ? {} : await readBody(req)
          let [, , name, status] = hit
          const requestedCase = req.headers['x-treg-fixture-case']
          if (requestedCase !== undefined) {
            const cases = CASES[name]
            const selected = typeof requestedCase === 'string' && cases && Object.hasOwn(cases, requestedCase)
              ? cases[requestedCase] : undefined
            if (!selected) throw new FixtureRequestError(400, 'invalid_request', 'Unknown fixture case for this route')
            name = selected[0]; status = selected[1]
          } else if (body.dry_run === true && name === 'runInstall.handle') {
            name = 'runInstall.dryRun'; status = 200
          } else if (body.dry_run === true && name === 'createSchemaChange.default') {
            name = 'createSchemaChange.preflight'; status = 200
          }
          const file = `${FIXTURES}${name}.json`
          if (!existsSync(file)) return respond(res, 404, { detail: `no fixture for ${req.method} ${path}`, code: 'not_found' })
          const payload = JSON.parse(readFileSync(file, 'utf8'))
          respond(res, status, req.method === 'GET' ? paginate(payload, url, hit[4]) : payload)
        } catch (error) {
          if (error instanceof FixtureRequestError) respond(res, error.status, { detail: error.message, code: error.code })
          else next(error)
        }
      })
    },
  }
}
