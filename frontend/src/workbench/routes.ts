export type WorkbenchTab = 'tables' | 'runs' | 'triggers'

export interface WorkbenchRoute {
  tab: WorkbenchTab
  tableId: string | null
  runId: string | null
  cell: { row: string, column: string } | null
  panel: 'sql' | 'add' | null
}

export const WORKBENCH_TABS: { key: WorkbenchTab, label: string }[] = [
  { key: 'tables', label: 'Tables' },
  { key: 'runs', label: 'Runs' },
  { key: 'triggers', label: 'Triggers' },
]

const ID = '[A-Za-z0-9][A-Za-z0-9._:-]{0,99}'
const HASH = new RegExp(`^#?workbench(?:/(tables|runs|triggers)(?:/(${ID})(?:/(sql|add))?)?)?(?:\\?(.*))?$`)

export function parseWorkbenchHash(hash: string): WorkbenchRoute | null {
  const m = HASH.exec(hash || '')
  if (!m) return null
  const tab = (m[1] || 'tables') as WorkbenchTab
  const id = m[2] || null
  const params = new URLSearchParams(m[4] || '')
  const cellParam = params.get('cell')
  const cellMatch = cellParam ? /^([^:]+):(.+)$/.exec(cellParam) : null
  return {
    tab,
    tableId: tab === 'tables' ? id : null,
    runId: tab === 'runs' ? id : null,
    cell: tab === 'tables' && id && cellMatch ? { row: cellMatch[1], column: cellMatch[2] } : null,
    panel: tab === 'tables' && id && (m[3] === 'sql' || m[3] === 'add') ? m[3] : null,
  }
}

export function workbenchHash(route: Partial<WorkbenchRoute>): string {
  const tab = route.tab || 'tables'
  let hash = `#workbench/${tab}`
  if (tab === 'tables' && route.tableId) {
    hash += `/${encodeURIComponent(route.tableId)}`
    if (route.panel) hash += `/${route.panel}`
    else if (route.cell) hash += `?cell=${encodeURIComponent(route.cell.row)}:${encodeURIComponent(route.cell.column)}`
  } else if (tab === 'runs' && route.runId) {
    hash += `/${encodeURIComponent(route.runId)}`
  }
  return hash
}

export const EMPTY_ROUTE: WorkbenchRoute = { tab: 'tables', tableId: null, runId: null, cell: null, panel: null }
