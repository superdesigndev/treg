// A hub tool's inputs (recipe.json `inputs`) as form fields, and the form's values back as the run
// body. The server checks again and names the field on a 422; this only saves a round trip.

export type InputSpec = {
  type?: string
  default?: unknown
  example?: unknown
  note?: string
  min?: number
  max?: number
  secret?: boolean
}

export type Field = {
  name: string
  label: string
  control: 'text' | 'textarea' | 'secret' | 'number' | 'toggle' | 'lines' | 'json'
  type: string
  required: boolean
  help: string
  placeholder: string
  min?: number
  max?: number
  step?: string
}

export function label(name: string): string {
  const words = name.replace(/[_-]+/g, ' ').trim()
  return words ? words[0].toUpperCase() + words.slice(1) : name
}

function show(v: unknown): string {
  if (v === undefined || v === null) return ''
  return typeof v === 'string' ? v : JSON.stringify(v)
}

export function fields(inputs: Record<string, InputSpec>): Field[] {
  return Object.entries(inputs || {}).map(([name, spec]) => {
    const type = spec.type || 'string'
    const long = String(spec.example ?? '').length > 60 || String(spec.default ?? '').length > 60
    const control: Field['control'] =
      type === 'int' || type === 'float' ? 'number'
        : type === 'bool' ? 'toggle'
          : type === 'object' ? 'json'
            : type === 'list' ? (Array.isArray(spec.example) && spec.example.some(x => x && typeof x === 'object') ? 'json' : 'lines')
              : spec.secret ? 'secret' : long ? 'textarea' : 'text'
    return {
      name, label: label(name), control, type,
      required: !('default' in spec),
      help: spec.note || '',
      placeholder: control === 'lines' && Array.isArray(spec.example) ? spec.example.join('\n') : show(spec.example),
      min: spec.min, max: spec.max, step: type === 'int' ? '1' : type === 'float' ? 'any' : undefined,
    }
  })
}

// What the form starts with: the default where there is one. A secret never starts filled.
export function initial(inputs: Record<string, InputSpec>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const f of fields(inputs)) {
    const spec = inputs[f.name]
    const d = spec.default
    if (f.control === 'toggle') out[f.name] = d === undefined ? false : Boolean(d)
    else if (f.control === 'lines') out[f.name] = Array.isArray(d) ? d.join('\n') : ''
    else if (f.control === 'json') out[f.name] = d === undefined ? '' : JSON.stringify(d, null, 2)
    else if (f.control === 'secret') out[f.name] = ''
    else out[f.name] = d === undefined || d === null ? '' : String(d)
  }
  return out
}

export type Built = { body: Record<string, unknown>, errors: Record<string, string> }

// The form's values as the run body. An empty optional field is left out, so the tool's own default
// applies; an empty required one is an error.
export function build(inputs: Record<string, InputSpec>, values: Record<string, unknown>): Built {
  const body: Record<string, unknown> = {}
  const errors: Record<string, string> = {}
  for (const f of fields(inputs)) {
    const raw = values[f.name]
    if (f.control === 'toggle') { body[f.name] = Boolean(raw); continue }
    const text = typeof raw === 'string' ? raw.trim() : raw === undefined || raw === null ? '' : String(raw)
    if (text === '') {
      if (f.required) errors[f.name] = 'Required'
      continue
    }
    if (f.control === 'number') {
      const n = Number(text)
      if (!Number.isFinite(n)) { errors[f.name] = 'A number'; continue }
      if (f.type === 'int' && !Number.isInteger(n)) { errors[f.name] = 'A whole number'; continue }
      if (f.min !== undefined && n < f.min) { errors[f.name] = `At least ${f.min}`; continue }
      if (f.max !== undefined && n > f.max) { errors[f.name] = `At most ${f.max}`; continue }
      body[f.name] = n
    } else if (f.control === 'lines') {
      body[f.name] = text.split('\n').map(s => s.trim()).filter(Boolean)
    } else if (f.control === 'json') {
      try {
        const v = JSON.parse(text)
        if (f.type === 'object' && (v === null || typeof v !== 'object' || Array.isArray(v))) { errors[f.name] = 'A JSON object'; continue }
        if (f.type === 'list' && !Array.isArray(v)) { errors[f.name] = 'A JSON list'; continue }
        body[f.name] = v
      } catch { errors[f.name] = 'Valid JSON' }
    } else {
      body[f.name] = text
    }
  }
  return { body, errors }
}
