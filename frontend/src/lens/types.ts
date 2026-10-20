export const LENS_ENGINES = [
  { id: 'chatgpt', label: 'ChatGPT' }, { id: 'perplexity', label: 'Perplexity' },
  { id: 'gemini', label: 'Gemini' }, { id: 'google', label: 'Google AI' },
] as const
export type LensEngine = typeof LENS_ENGINES[number]['id']
export interface Competitor { name: string; domain?: string; reason?: string }
export interface LensProfile {
  brand: string; url: string; description: string; competitors: Competitor[]; questions: string[]; engines: LensEngine[]
}
export interface LensProject extends LensProfile {
  id: string; revision: number; can_edit: boolean; created_at: string; updated_at: string
}
export interface LensQuote {
  id: string; run_id: string; kind: 'onboarding' | 'audit'; total_cost_micro: number
  expires_at: string; note: string; currency: 'USD'; input: Record<string, unknown>; project_id?: string | null; project_revision?: number | null
}
export interface LensSource { title: string; url: string; domain?: string }
export interface LensAnswer {
  id: string; question: string; engine: LensEngine; answer: string; citations: LensSource[]
  mentioned_brands: string[]; brand_mentioned: boolean; position: number | null
  charged_micro: number | null; cost_known: boolean; source_kind: 'web-interface' | 'model-api'
  provider: string; completed_at: string; truncated?: boolean
}
export interface LensResult {
  kind: 'onboarding' | 'audit'; input: Record<string, unknown>; results: LensAnswer[]
  research?: { brand: string; description: string; competitors: Competitor[]; questions: string[]; sources: LensSource[] }
  errors: { engine?: string; question?: string; detail: string; call_ref?: string; charged_micro?: number | null }[]
  known_cost_micro: number; total_cost_micro: number | null; cost_known: boolean; completed_at: string
  attempts?: { id: string; endpoint_id: string; engine?: string; question?: string; state: string; charged_micro: number | null; call_ref?: string; detail?: string }[]
}
export interface LensRun {
  id: string; kind: 'onboarding' | 'audit'; project_id?: string | null
  state: 'quoted' | 'expired' | 'running' | 'completed' | 'failed' | 'interrupted'
  created_at: string; completed_at?: string | null; result: LensResult | null; error?: string | null; quote?: LensQuote
}
export interface LensScope { user: string; team: string; enabled: boolean; canWrite?: boolean; headers: Record<string, string> }
export interface PendingRun { id: string; kind: 'onboarding' | 'audit' }
export interface LensDraft {
  version: 1; requestId: string; mode: 'setup' | 'workspace'; step: 'website' | 'brand' | 'questions'
  form: LensProfile; selectedQuestions: boolean[]; editingId: string | null; editingRevision: number | null; selectedId: string | null
  appliedResearchId: string | null; pending: PendingRun | null; receipt: LensRun | null; uncertainSave: boolean
}
