export type Attachment = { name: string, size?: number, text?: string }
export type Msg = {
  id: number, role: 'user' | 'assistant' | 'tool' | 'event', at: string,
  text?: string, calls?: string[], cost_micro?: number, stopped?: boolean, attachments?: Attachment[],
  name?: string, summary?: string, ok?: boolean, args?: any, result?: string, version?: number | null, prev_version?: number | null,
  kind?: string, detail?: any, inputs?: any, tool_id?: string, status?: number | string, url?: string,
}
export type Session = { id: number, title: string, tool_id: string | null, updated_at: string, trimmed: boolean, pinned: boolean, busy: boolean,
  has_files?: boolean, name?: string | null }
export type Draft = { manifest?: any, script?: string, check?: any, readme?: string, data?: string }
export type Pending = { id: string, kind: 'test' | 'publish' | 'app' | 'password', inputs?: any, est_cost_usd?: number | null, name?: string | null }
export type Conversation = Session & { draft: Draft, summary: string | null, pending: Pending | null, auto_test: boolean, messages: Msg[] }
export type ToolStatus = {
  tool_id: string, version: number, status: string, live_version: number | null, health: string, listing: string | null,
  runs_30d: number, price_label: string, share_url: string, call: string,
  app: { enabled: boolean, name: string, url: string, password: boolean, locked: boolean } | null,
}
export type Warning = { kind: 'access', id: string, why_not?: string, fix?: string } | { kind: 'balance', balance_micro: number, need_micro: number }

export const STEP_WORDS: Record<string, (a: any) => string> = {
  catalog_search: a => `Searching the catalog for “${a?.query ?? ''}”`,
  catalog_get: a => `Reading ${a?.id ?? 'a tool'}`,
  my_tools: () => 'Listing your team\'s tools',
  load_my_tool: a => `Loading ${a?.tool_id ?? 'your tool'}`,
  read_files: a => `Reading ${a?.file ?? 'a file'}`,
  write_files: () => 'Writing the files',
  test_run: () => 'Getting a test run ready',
  publish: () => 'Getting ready to publish',
  app_on: () => 'Getting the app ready',
  app_password: () => 'Asking for the app password',
}
